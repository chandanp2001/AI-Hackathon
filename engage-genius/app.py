# Fix SSL certificate issues
import os
import ssl
os.environ['PYTHONHTTPSVERIFY'] = '0'
ssl._create_default_https_context = ssl._create_unverified_context

# Workaround for SSL verification issues on macOS
os.environ['SSL_CERT_FILE'] = '/etc/ssl/cert.pem'

from flask import Flask, request, jsonify
import asyncio
from services.data_sources.base import DataSourceRegistry
from services.data_sources.slack_source import SlackDataSource
from services.data_sources.jira_source import JiraDataSource
from services.data_sources.google_docs_source import GoogleDocsDataSource
from services.data_sources.google_sheets_source import GoogleSheetsDataSource
from services.data_sources.tableau_source import TableauDataSource
from services.data_sources.devrev_source import DevRevDataSource
from services.flow_controller.controller import FlowController
from services.llm.context_manager import ContextManager
from services.ui.slack_handler import SlackHandler
from services.llm.llm_service import LLMService
from services.apollo_command import ApolloCommand
from asgiref.sync import async_to_sync
import logging
import json
import threading
import time  # Make sure time is imported
from slack_sdk.errors import SlackApiError
import argparse
from slack_sdk import WebClient
from services.auth.google_auth import setup_google_auth
from datetime import datetime
import requests
import functools
from concurrent.futures import ThreadPoolExecutor

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Initialize components
source_registry = DataSourceRegistry()
source_registry.register("slack", SlackDataSource())

# Register JIRA data source with improved error handling
try:
    logger.info("Initializing JIRA data source...")
    jira_source = JiraDataSource()
    
    # Check if JIRA source has connection errors
    if hasattr(jira_source, 'connection_error') and jira_source.connection_error:
        logger.error(f"JIRA connection error: {jira_source.connection_error}")
        logger.warning("JIRA data source registered but will not be fully functional")
    else:
        logger.info("JIRA data source initialized successfully")
        
    # Register the source anyway so the UI can show appropriate error messages
    source_registry.register("jira", jira_source)
except Exception as e:
    logger.error(f"Failed to initialize JIRA data source: {str(e)}")
    logger.warning("JIRA features will not be available")

# Register DevRev data source with similar error handling
try:
    logger.info("Initializing DevRev data source...")
    devrev_source = DevRevDataSource()
    
    # Check if DevRev source has connection errors
    if hasattr(devrev_source, 'connection_error') and devrev_source.connection_error:
        logger.error(f"DevRev connection error: {devrev_source.connection_error}")
        logger.warning("DevRev data source registered but will not be fully functional")
    else:
        logger.info("DevRev data source initialized successfully")
        
    # Register the source anyway so the UI can show appropriate error messages
    source_registry.register("devrev", devrev_source)
except Exception as e:
    logger.error(f"Failed to initialize DevRev data source: {str(e)}")
    logger.warning("DevRev features will not be available")

# Before initializing Google services
try:
    logger.info("Setting up Google authentication...")

    def get_credentials_path():
        """Find the Google API credentials file with improved deployment support"""
        logger.info("Searching for Google credentials file...")
        
        # First check environment variable (highest priority)
        if os.environ.get('GOOGLE_CREDENTIALS_PATH'):
            env_path = os.environ.get('GOOGLE_CREDENTIALS_PATH')
            logger.info(f"Found credentials path in environment variable: {env_path}")
            if os.path.exists(env_path):
                return os.path.abspath(env_path)
            else:
                logger.warning(f"Environment variable path does not exist: {env_path}")
        
        # Common deployment locations
        credentials_paths = [
            'credentials.json',  # Direct path in current directory
            os.path.join('..', '..', 'credentials.json'),  # Two directories up
            os.path.abspath(os.path.join(os.path.dirname(__file__), 'credentials.json')),  # Same dir as script
            '/app/credentials.json',  # Common Docker container path
            '/etc/engage-genius/credentials.json',  # System config location
            os.path.join(os.path.expanduser('~'), '.config', 'engage-genius', 'credentials.json'),  # User config
        ]
        
        # Find first valid path
        for path in credentials_paths:
            if os.path.exists(path):
                logger.info(f"Found credentials at: {path}")
                return os.path.abspath(path)
        
        logger.error("Could not find credentials.json in any location")
        return None
    
    # Get credentials path
    credentials_path = get_credentials_path()
    
    if credentials_path:
        logger.info(f"Found credentials at: {credentials_path}")
        
        # Set environment variables for all components to use
        os.environ['GOOGLE_CREDENTIALS_PATH'] = credentials_path
        os.environ['GOOGLE_TOKEN_PATH'] = os.path.join(os.path.dirname(credentials_path), 'token.json')
        
        # Call setup_google_auth
        setup_google_auth()
        
        # Now register Google services
        source_registry.register("google_docs", GoogleDocsDataSource())
        source_registry.register("google_sheets", GoogleSheetsDataSource())
        logger.info("Successfully registered Google services")
    else:
        logger.warning("credentials.json not found in any location - Google services will not be available")
except Exception as e:
    logger.error(f"Failed to register Google services: {str(e)}")
    logger.warning("Google services will not be available")

# Register Tableau service
try:
    logger.info("Initializing Tableau data source...")
    tableau_source = TableauDataSource()
    
    # Check if Tableau source has connection errors
    if hasattr(tableau_source, 'connection_error') and tableau_source.connection_error:
        logger.error(f"Tableau connection error: {tableau_source.connection_error}")
        logger.warning("Tableau data source registered but will not be fully functional")
    else:
        logger.info("Tableau data source initialized successfully")
    
    # Register the Tableau data source
    source_registry.register("tableau", tableau_source)
    logger.info("Successfully registered Tableau service")
except Exception as e:
    logger.error(f"Failed to initialize Tableau data source: {str(e)}")
    logger.warning("Tableau features will not be available")

# Initialize LLM service
llm_service = LLMService()

flow_controller = FlowController(source_registry)
context_manager = ContextManager(source_registry, llm_service)
slack_handler = SlackHandler(flow_controller, source_registry, context_manager)

# Initialize the Apollo command handler
apollo_command = ApolloCommand()

# Function to run the channel prefetch in the background
def prefetch_channels_background():
    """Run the channel prefetch in a background thread to avoid blocking startup"""
    logger.info("Starting background thread for channel prefetch")
    
    async def _run_prefetch():
        try:
            logger.info("Running channel prefetch operation")
            await slack_handler.prefetch_channels()
            logger.info("Channel prefetch completed")
        except Exception as e:
            logger.error(f"Error in channel prefetch: {e}")
    
    # Create a new event loop for this thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # Run the prefetch
    loop.run_until_complete(_run_prefetch())
    loop.close()
    
    logger.info("Background channel prefetch thread finished")

# Function to run the Tableau views prefetch in the background
def prefetch_tableau_views_background():
    """Run the Tableau views prefetch in a background thread to avoid blocking startup"""
    logger.info("Starting background thread for Tableau views prefetch")
    
    async def _run_prefetch():
        try:
            logger.info("Running Tableau views prefetch operation")
            # Cast to the proper type to access the method
            tableau_ds = source_registry.get_source("tableau")
            if tableau_ds and hasattr(tableau_ds, 'prefetch_views_async'):
                await tableau_ds.prefetch_views_async()
                logger.info("Tableau views prefetch completed")
            else:
                logger.warning("Tableau data source not available or doesn't support async prefetching")
        except Exception as e:
            logger.error(f"Error in Tableau views prefetch: {e}")
    
    # Create a new event loop for this thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # Run the prefetch
    loop.run_until_complete(_run_prefetch())
    loop.close()
    
    logger.info("Background Tableau views prefetch thread finished")

# Thread pool for concurrent processing
executor = ThreadPoolExecutor(max_workers=10)

# Utility function for Flask async responses
def async_response(f):
    """
    Decorator to make Flask handlers return immediately while processing continues in background.
    This helps with Slack's 3-second timeout by prioritizing the acknowledgment response.
    """
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        # Get the acknowledgment response first
        start_time = time.time()
        acknowledgment = f(*args, **kwargs)
        logger.info(f"Async response generated in {time.time() - start_time:.5f}s")
        return acknowledgment
    return wrapper

@app.route("/", methods=["GET"])
def home():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "message": "Slack bot is running"
    })

@app.route("/slack/webhook", methods=["POST"])
def root_webhook_challenge():
    """Handle Slack URL verification at root endpoint and forward events to the proper endpoint"""
    try:
        # Parse the event data
        event_data = request.json
        logger.info(f"Received webhook at root: {event_data}")
        
        # Handle URL verification challenge
        if event_data and event_data.get("type") == "url_verification":
            challenge = event_data.get("challenge", "")
            logger.info(f"Returning challenge value: {challenge}")
            return jsonify({"challenge": challenge})
            
        # For actual Slack events, forward them to the proper handler
        # This helps when Slack sends events to the root URL instead of /slack/events
        if event_data and "event" in event_data:
            logger.info("Forwarding event from root to /slack/events handler")
            # Call the actual event handler directly
            return handle_events()
        
        return jsonify({"error": "Unsupported event type"}), 400
    except Exception as e:
        logger.error(f"Error handling root webhook: {str(e)}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/health", methods=["GET"])
def health_check():
    """Detailed health check endpoint"""
    health_status = {
        "app": {
            "status": "healthy",
            "timestamp": str(datetime.now())
        },
        "data_sources": {}
    }
    
    # Check data sources
    for source_name in source_registry.get_sources():
        source = source_registry.get_source(source_name)
        if source:
            try:
                # Call health_check method if available
                if hasattr(source, 'health_check') and callable(getattr(source, 'health_check')):
                    health_status["data_sources"][source_name] = source.health_check()
                else:
                    health_status["data_sources"][source_name] = {"status": "unknown", "message": "No health check available"}
            except Exception as e:
                health_status["data_sources"][source_name] = {
                    "status": "error",
                    "error": str(e)
                }
    
    return jsonify(health_status)

@app.route("/slack/command", methods=["POST"])
def handle_slash_command():
    """Handle Slack slash commands"""
    try:
        # Add timing metrics - start time
        start_time = time.time()
        logger.info(f"Slash command received at {start_time}")
        
        logger.info(f"Received slash command. Data: {request.form}")
        
        if not request.form:
            logger.error("No form data received")
            return jsonify({
                "text": "Error: No data received with the command"
            }), 400

        # Check which command was used
        command = request.form.get('command', '').strip()
        
        # Different handling based on command type
        if command == '/createapollo':
            logger.info("Handling /createapollo command")
            return handle_createapollo_command(request.form)
        else:
            # Default to handling /test command
            # Log time after initial processing
            initial_processing_time = time.time()
            logger.info(f"Initial processing completed in {initial_processing_time - start_time:.5f}s")

            # CRITICAL: Immediately acknowledge the request to prevent Slack timeout
            # Send an immediate response to avoid Slack's 3-second timeout
            acknowledgement = {
                "text": "Processing your request... 🔄"
            }
            
            # PRIORITY: Create and return the acknowledgment immediately to avoid timeout
            response_start = time.time()
            response = jsonify(acknowledgement)
            response_time = time.time() - response_start
            logger.info(f"Response created in {response_time:.5f}s")
            
            # Total time until returning response
            total_time = time.time() - start_time
            logger.info(f"TOTAL TIME TO ACKNOWLEDGE: {total_time:.5f}s")
            
            # After response is sent, process in background
            # Add timing for background thread creation
            thread_setup_start = time.time()
            
            # Copy form data and start background processing
            form_data_copy = request.form.copy()
            
            # Create and start background thread through global executor to limit concurrency
            executor.submit(process_command_async, form_data_copy)
            
            thread_setup_end = time.time()
            logger.info(f"Thread setup completed in {thread_setup_end - thread_setup_start:.5f}s")
            
            # Return immediate acknowledgment
            return response
        
    except Exception as e:
        error_time = time.time() - start_time if 'start_time' in locals() else -1
        logger.error(f"Error handling slash command after {error_time:.5f}s: {str(e)}", exc_info=True)
        return jsonify({
            "text": f"Sorry, something went wrong: {str(e)}"
        }), 500

def handle_createapollo_command(form_data):
    """Handle the /createapollo command"""
    try:
        # Immediately acknowledge to prevent Slack timeout
        acknowledgement = {
            "text": "Generating Apollo insights, this may take some time... ⏳"
        }
        
        # Return acknowledgment immediately to avoid timeout
        response = jsonify(acknowledgement)
        
        # Process command in background
        form_data_copy = form_data.copy()
        executor.submit(process_apollo_command_async, form_data_copy)
        
        return response
        
    except Exception as e:
        logger.error(f"Error handling Apollo command: {str(e)}", exc_info=True)
        return jsonify({
            "text": f"Sorry, something went wrong: {str(e)}"
        }), 500

# Move the background processing function outside the main handler
def process_command_async(form_data):
    """Process the slash command asynchronously in a background thread"""
    try:
        start_time = time.time()
        processing_start = time.time()
        logger.info(f"Background processing started at {processing_start:.5f}s")
        
        # Create a new event loop for the thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # Process the slash command
        logger.info("Starting slash command processing")
        response = asyncio.run(slack_handler.handle_slash_command(form_data))
        logger.info(f"Slash command processing completed in {time.time() - processing_start:.5f}s")
        
        # Send response via Slack API since we already acknowledged
        if 'response_url' in form_data:
            try:
                logger.info(f"Sending response to response_url")
                response_start = time.time()
                requests.post(
                    form_data['response_url'],
                    json=response,
                    headers={"Content-Type": "application/json"}
                )
                logger.info(f"Response sent in {time.time() - response_start:.5f}s")
            except Exception as e:
                logger.error(f"Failed to send response to response_url: {e}")
        else:
            # If no response_url, try to send to channel
            if 'channel_id' in form_data:
                logger.info(f"Sending response to channel_id")
                channel_start = time.time()
                asyncio.run(slack_handler.post_message(
                    form_data['channel_id'],
                    text=response.get('text'),
                    blocks=response.get('blocks')
                ))
                logger.info(f"Channel message sent in {time.time() - channel_start:.5f}s")
                
        loop.close()
        logger.info(f"Total background processing time: {time.time() - processing_start:.5f}s")
    except Exception as e:
        logger.error(f"Error in background processing: {str(e)}", exc_info=True)
        # Try to send error message back to user
        if 'response_url' in form_data:
            try:
                requests.post(
                    form_data['response_url'],
                    json={"text": f"Sorry, something went wrong: {str(e)}"},
                    headers={"Content-Type": "application/json"}
                )
            except:
                pass

def process_apollo_command_async(form_data):
    """Process the Apollo command asynchronously"""
    try:
        # Log the incoming form data for debugging
        logger.info(f"Processing Apollo command with data: {form_data}")
        channel_id = form_data.get('channel_id')
        logger.info(f"Command issued in channel_id: {channel_id}")
        
        # Use the ThreadPoolExecutor to run the async command in a separate thread
        # This avoids event loop issues
        def run_apollo_command():
            # Create a new event loop for the thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            # Handle the Apollo command
            response = loop.run_until_complete(apollo_command.handle(form_data))
            loop.close()
            return response
        
        # Run in the executor and get the response
        with ThreadPoolExecutor(max_workers=1) as executor:
            response = executor.submit(run_apollo_command).result()
        
        # Log the response for debugging
        logger.info(f"Apollo response: {response}")
        
        # Make sure we use the channel ID from the original form data if available
        channel_id = form_data.get('channel_id')
        if not channel_id:
            channel_id = response.get('channel')
            logger.info(f"Using channel ID from response: {channel_id}")
        else:
            logger.info(f"Using channel ID from form data: {channel_id}")
        
        # Initialize Slack client
        from slack_sdk import WebClient
        from config import SLACK_BOT_TOKEN
        client = WebClient(token=SLACK_BOT_TOKEN)
        
        # Post message directly to the channel
        result = client.chat_postMessage(
            channel=channel_id,
            text="Apollo Report",
            blocks=response.get('blocks'),
            thread_ts=form_data.get('thread_ts') or response.get('thread_ts')
        )
        
        logger.info(f"Message posted to channel {channel_id}, result: {result.get('ok', False)}")
        
        logger.info("Apollo command processing completed")
        
    except Exception as e:
        logger.error(f"Error in Apollo command processing: {str(e)}", exc_info=True)
        # Try to send error message back to user
        if 'response_url' in form_data:
            try:
                requests.post(
                    form_data['response_url'],
                    json={"text": f"Error generating Apollo insights: {str(e)}"},
                    headers={"Content-Type": "application/json"}
                )
            except Exception as error:
                logger.error(f"Failed to send error message: {error}")
                
        # Also try to post error directly to channel
        try:
            channel_id = form_data.get('channel_id')
            if channel_id:
                from slack_sdk import WebClient
                from config import SLACK_BOT_TOKEN
                client = WebClient(token=SLACK_BOT_TOKEN)
                
                client.chat_postMessage(
                    channel=channel_id,
                    text=f"Error generating Apollo insights: {str(e)}"
                )
        except Exception as error:
            logger.error(f"Failed to post error to channel: {error}")

@app.route("/slack/interact", methods=["POST"])
def handle_interaction():
    """Handle Slack interactive components"""
    try:
        logger.info("Received interaction")
        logger.info(f"Request form keys: {list(request.form.keys())}")
        
        # Check what type of interaction this is based on the request format
        interaction_data = None
        
        # Option 1: Direct external_select menu options load request
        if 'payload' not in request.form and request.form.get('type') in ['block_suggestions']:
            logger.info("Processing direct block_suggestions request")
            interaction_data = request.form.to_dict()
            
        # Option 2: Form with payload parameter (standard interactions)
        elif 'payload' in request.form:
            logger.info("Processing standard payload interaction")
            payload = request.form.get('payload', '{}')
            if not payload:
                logger.error("Empty payload received")
                return jsonify({"text": "Error: Empty interaction payload"}), 400
            
            interaction_data = json.loads(payload)
            
            # Check if we have a user token and not registered it yet
            user_id = interaction_data.get('user', {}).get('id')
            user_token = None
            
            # Try to extract user token from request if available
            # Note: This depends on the Slack API sending user tokens with interactions
            # In a real implementation, you might need to store user tokens in a database
            # after OAuth flow
            if user_id and request.headers.get('Authorization'):
                auth_header = request.headers.get('Authorization', '')
                if auth_header.startswith('Bearer '):
                    user_token = auth_header[7:]  # Remove 'Bearer ' prefix
                    logger.info(f"Found user token for {user_id} in Authorization header")
                    
                    # Register user token with the SlackDataSource
                    try:
                        # This is async, so we need to use async_to_sync
                        async_to_sync(slack_handler.register_user_token)(user_id, user_token)
                    except Exception as e:
                        logger.warning(f"Error registering user token: {e}")
            
        # Option 3: Unknown format - try to use whatever we have
        else:
            logger.warning("Unknown interaction format - using entire form data")
            interaction_data = request.form.to_dict()
            
        if not interaction_data:
            logger.error("Could not extract interaction data from request")
            return jsonify({"text": "Error: Could not parse interaction data"}), 400
            
        logger.info(f"Interaction data type: {interaction_data.get('type', 'unknown')}")
        logger.info(f"Interaction data keys: {list(interaction_data.keys())}")
        
        try:
            response = async_to_sync(slack_handler.handle_interaction)(interaction_data)
            logger.info(f"Sending response: {response}")
            return jsonify(response)
        except SlackApiError as e:
            # Special handling for Slack API errors
            if "ratelimited" in str(e).lower():
                logger.warning(f"Rate limited by Slack API: {str(e)}")
                # Provide a friendly message to the user
                return jsonify({
                    "text": "We're experiencing high traffic. Please try again in a few seconds or type the channel name to search instead of browsing all channels."
                })
            else:
                # Re-raise for generic error handling
                raise e
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON payload: {str(e)}")
        return jsonify({
            "text": "Error: Invalid interaction payload"
        }), 400
    except Exception as e:
        logger.error(f"Error handling interaction: {str(e)}", exc_info=True)
        return jsonify({
            "text": f"Sorry, something went wrong: {str(e)}"
        }), 500

@app.route("/slack/events", methods=["POST"])
def handle_events():
    """Handle Slack events, including message events for follow-up queries"""
    try:
        # Parse the event data
        event_data = request.json
        logger.info(f"Received Slack event: {event_data.get('type', 'unknown')}")
        
        # Add detailed logging for debugging
        if "event" in event_data:
            event = event_data.get("event", {})
            event_type = event.get("type")
            has_thread = "thread_ts" in event
            is_bot = "bot_id" in event or event.get("subtype") == "bot_message"
            logger.info(f"EVENT DETAILS: type={event_type}, thread={has_thread}, is_bot={is_bot}, user={event.get('user')}")
            if has_thread:
                logger.info(f"THREAD DETAILS: thread_ts={event.get('thread_ts')}, channel={event.get('channel')}")
                
            # Log full event data for debugging
            logger.debug(f"FULL EVENT DATA: {json.dumps(event_data)}")
        
        # Handle URL verification challenge
        if event_data.get("type") == "url_verification":
            logger.info(f"Handling URL verification challenge: {event_data.get('challenge')}")
            # Return the challenge value directly as required by Slack
            return jsonify({"challenge": event_data.get("challenge", "")})
            
        # Process events
        event = event_data.get("event", {})
        event_type = event.get("type")
        
        # Process message events for follow-up queries
        if event_type == "message":
            # Check if it's a bot message (to avoid loops)
            if "bot_id" in event or event.get("subtype") == "bot_message":
                logger.debug("Ignoring bot message")
                return jsonify({"ok": True})
            
            # Process message in a background thread to respond quickly to Slack
            logger.info(f"Starting background processing for message event from user {event.get('user')}")
            executor.submit(process_message_event_async, event)
            
        return jsonify({"ok": True})
        
    except Exception as e:
        logger.error(f"Error handling Slack event: {str(e)}", exc_info=True)
        return jsonify({"error": str(e)}), 500
        
def process_message_event_async(event):
    """Process message event asynchronously to avoid timeouts"""
    try:
        logger.info(f"Starting message event processing for user {event.get('user')} in channel {event.get('channel')}")
        
        # Log if this is a thread message
        if 'thread_ts' in event:
            logger.info(f"Processing thread message with thread_ts={event.get('thread_ts')}")
        
        # Create a new event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # Call the async handler
        logger.info(f"Calling slack_handler.handle_message_event")
        loop.run_until_complete(slack_handler.handle_message_event(event))
        loop.close()
        
        logger.info(f"Successfully processed message event from user {event.get('user')}")
    except Exception as e:
        logger.error(f"Error processing message event: {str(e)}", exc_info=True)

@app.errorhandler(404)
def not_found(e):
    """Handle 404 errors"""
    logger.warning(f"404 error: {request.url}")
    return jsonify({
        "error": "Not found",
        "message": f"The requested URL {request.url} was not found on this server.",
        "hint": "Make sure you're using /slack/command for slash commands and /slack/interact for interactions"
    }), 404

@app.errorhandler(500)
def server_error(e):
    """Handle 500 errors"""
    logger.error(f"500 error: {str(e)}")
    return jsonify({
        "error": "Internal server error",
        "message": "An unexpected error occurred"
    }), 500

# Register the default user token for testing (remove in production)
def register_default_token():
    """Register the default user token from config"""
    try:
        from config import SLACK_USER_TOKEN
        
        if not SLACK_USER_TOKEN:
            app.logger.warning("No SLACK_USER_TOKEN found in config. Search API functionality will be limited.")
            app.logger.info("To enable search, add a user token to your .env file as SLACK_USER_TOKEN.")
            app.logger.info("Get a token from https://api.slack.com/apps > Your App > OAuth & Permissions > User Token Scopes")
            app.logger.info("Required scope: search:read")
            return
        
        # Strip any quotes that might have been included in the token
        clean_token = SLACK_USER_TOKEN.strip().strip('"\'')
        app.logger.info(f"Attempting to register token from config: {clean_token[:10]}...")
            
        user_id = "default_user"  # Use a default user ID
        
        # Register the token with the slack handler
        result = async_to_sync(slack_handler.register_user_token)(user_id, clean_token)
        if result:
            app.logger.info(f"Successfully registered user token from config for search operations")
        else:
            app.logger.warning(f"Failed to register default token from config - token may be invalid")
            app.logger.info("Please check your SLACK_USER_TOKEN in .env file and ensure it has the search:read scope")
            app.logger.info("Search API will fall back to message history which may be less accurate")
            
            # Try to validate why the token might be invalid
            try:
                test_client = WebClient(token=clean_token)
                try:
                    test_response = test_client.auth_test()
                    if not test_response.get('ok'):
                        error_msg = test_response.get('error', 'unknown error')
                        app.logger.error(f"Token validation failed: {error_msg}")
                        
                        if error_msg == 'invalid_auth':
                            app.logger.info("The token appears to be invalid or expired. Generate a new one from the Slack API dashboard.")
                        elif error_msg == 'missing_scope':
                            app.logger.info("The token is missing required scopes. Make sure it has the search:read scope.")
                        elif error_msg == 'token_revoked':
                            app.logger.info("The token has been revoked. Generate a new one from the Slack API dashboard.")
                except SlackApiError as e:
                    app.logger.error(f"Token validation error: {e}")
            except Exception as e:
                app.logger.error(f"Error during token validation: {e}")
    except Exception as e:
        app.logger.error(f"Error registering default token: {str(e)}")


    return None

if __name__ == "__main__":
    # Register the default token
    register_default_token()

    # Set up Google authentication
    try:
        logger.info("Setting up Google authentication...")
        auth_result = setup_google_auth()
        # Result doesn't matter as long as no exceptions were thrown
        logger.info("Google authentication completed")
    except Exception as e:
        logger.error(f"Error setting up Google authentication: {e}")
        logger.warning("Google services will not be available")
    
    
    # Parse command line arguments
    parser = argparse.ArgumentParser(description='Slack Integration Server')
    parser.add_argument('--no-prefetch', action='store_true', help='Disable channel prefetching at startup (for testing)')
    parser.add_argument('--no-tableau-prefetch', action='store_true', help='Disable Tableau views prefetching at startup')
    parser.add_argument('--port', type=int, default=8000, help='Port to run the server on')
    parser.add_argument('--debug', action='store_true', help='Run in debug mode')
    parser.add_argument('--host', type=str, default='0.0.0.0', help='Host to bind to')
    args = parser.parse_args()
    
    if not args.no_prefetch:
        logger.info("Starting background channel prefetch...")
        # Start the background prefetch when the app is initialized
        prefetch_thread = threading.Thread(target=prefetch_channels_background)
        prefetch_thread.daemon = True  # Make thread a daemon so it exits when main thread exits
        prefetch_thread.start()
        logger.info("Initiated background channel prefetch")
    else:
        logger.info("Channel prefetching disabled via command line")
    
    # Start Tableau views prefetch if not disabled
    if not args.no_tableau_prefetch and source_registry.get_source("tableau"):
        logger.info("Starting background Tableau views prefetch...")
        # Start the background prefetch for Tableau views
        tableau_prefetch_thread = threading.Thread(target=prefetch_tableau_views_background)
        tableau_prefetch_thread.daemon = True
        tableau_prefetch_thread.start()
        logger.info("Initiated background Tableau views prefetch")
    else:
        logger.info("Tableau views prefetching disabled or unavailable")

    logger.info(f"Starting Flask server on port {args.port}...")
    logger.info("Endpoints:")
    logger.info("  - /slack/command (POST): Handle slash commands")
    logger.info("  - /slack/interact (POST): Handle interactions")
    app.run(host=args.host, port=args.port, debug=args.debug)
