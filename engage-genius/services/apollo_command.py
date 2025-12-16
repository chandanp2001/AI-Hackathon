import logging
from typing import Dict, Any, Optional, List
from .google_service import fetch_google_sheets_data, fetch_google_doc_data
from .slack_service import fetch_slack_messages
from .jira_service import fetch_jira_project
from .data_sources.google_docs_source import GoogleDocsDataSource
from .data_sources.google_sheets_source import GoogleSheetsDataSource
import asyncio
import concurrent.futures
import json
import traceback
from .llm.llm_service import generate_ai_analysis

# Configure logging
logger = logging.getLogger(__name__)

# Slack API limitation constants
# Set a more conservative limit to account for overhead (2900 instead of 3000)
SLACK_MAX_TEXT_LENGTH = 2900
SLACK_MAX_BLOCKS = 50

class ApolloCommand:
    """Handler for the /createapollo Slack command"""
    
    def __init__(self):
        self.hard_coded_params = {
            "CHANNEL_ID_2": "C07Q18XM674",  # Channel ID for Slack messages (instead of channel name)
            "JIRA_PROJECT": "EHGI",               # JIRA project ID
            "GOOGLE_SHEETS_ID": "13R3WgzPsrXc1TvK6Q-CoIOre0M8NFINR4SLbpjhnX_4",  # Google Sheets ID
            "GOOGLE_SHEET_NAME": "Staff Review",  # Sheet name
            "GOOGLE_SHEET_RANGE": "A1:V7",        # Cell range
            "GOOGLE_DOC_ID": "11J0QhFRX2bK-I5SYw5_-U00xY38YckpwP6Knx6PnGUE"  # Google Doc ID
        }
        # Initialize data sources
        try:
            self.google_docs_source = GoogleDocsDataSource()
            self.google_sheets_source = GoogleSheetsDataSource()
            logger.info("Google data sources initialized successfully")
        except Exception as e:
            logger.error(f"Error initializing Google data sources: {str(e)}")
            logger.error(traceback.format_exc())
            raise
    
    async def handle(self, command_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle the /createapollo command
        
        Args:
            command_data: Dict with command data from Slack
            
        Returns:
            Dict with response data for Slack
        """
        try:
            logger.info(f"Handling /createapollo command with data: {command_data}")
            
            # Extract command text and arguments
            command_text = command_data.get("text", "").strip()
            
            # Regardless of subcommand, generate insights for all data sources
            return await self.generate_all_insights(command_data)
                
        except Exception as e:
            logger.error(f"Error handling Apollo command: {str(e)}")
            logger.error(traceback.format_exc())
            return {"text": f"Error: {str(e)}"}
    
    def _chunk_text_for_slack(self, text: str, max_length: int = SLACK_MAX_TEXT_LENGTH) -> List[str]:
        """Split text into chunks that fit Slack's block text limits
        
        Args:
            text: The text to split
            max_length: Maximum length per chunk
            
        Returns:
            List of text chunks
        """
        if not text:
            return ["No content available"]
            
        if len(text) <= max_length:
            return [text]
        
        chunks = []
        current_pos = 0
        
        while current_pos < len(text):
            # Find a good break point - prefer newline, then space, then just break at the limit
            chunk_end = min(current_pos + max_length, len(text))
            
            if chunk_end < len(text):
                # Look for natural breakpoints
                newline_pos = text.rfind('\n', current_pos, chunk_end)
                space_pos = text.rfind(' ', current_pos, chunk_end)
                
                if newline_pos > current_pos + max_length // 2:
                    # If we found a newline in the second half of the chunk, break there
                    chunk_end = newline_pos + 1  # Include the newline
                elif space_pos > current_pos + max_length // 2:
                    # If we found a space in the second half, break there
                    chunk_end = space_pos + 1  # Include the space
                # Otherwise just break at the limit
            
            # Make sure we're not exceeding the limit
            if chunk_end - current_pos > max_length:
                logger.warning(f"Chunk too long ({chunk_end - current_pos} chars), truncating to {max_length}")
                chunk_end = current_pos + max_length
                
            chunk_text = text[current_pos:chunk_end]
            logger.info(f"Adding chunk with {len(chunk_text)} characters")
            chunks.append(chunk_text)
            current_pos = chunk_end
        
        return chunks
    
    def _format_blocks_for_slack(self, insights: List[str]) -> List[Dict[str, Any]]:
        """Format insights into Slack blocks, splitting long text to respect Slack API limits
        
        Args:
            insights: List of insight texts
            
        Returns:
            List of Slack blocks
        """
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "GC Issuing Apollo Report",
                    "emoji": True
                }
            },
            {
                "type": "divider"
            }
        ]
        
        # Process each insight, splitting if needed
        for insight in insights:
            # If the text is too long, split it into chunks
            text_chunks = self._chunk_text_for_slack(insight)
            
            # Add each chunk as a separate block
            for chunk in text_chunks:
                # Double check the length before adding to blocks
                if len(chunk) > SLACK_MAX_TEXT_LENGTH:
                    logger.warning(f"Chunk still too long after splitting ({len(chunk)} chars), truncating")
                    chunk = chunk[:SLACK_MAX_TEXT_LENGTH - 100] + "... (content truncated due to length)"
                
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": chunk
                    }
                })
                # Don't add divider after each chunk, only after the full insight
                if chunk == text_chunks[-1]:
                    blocks.append({
                        "type": "divider"
                    })
        
        # Add footer
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Generated by Apollo | Data from Slack, Google Sheets, JIRA, and Google Docs"
                }
            ]
        })
        
        # Check total blocks and trim if needed
        if len(blocks) > SLACK_MAX_BLOCKS:
            logger.warning(f"Too many blocks ({len(blocks)}), trimming to {SLACK_MAX_BLOCKS}")
            blocks = blocks[:SLACK_MAX_BLOCKS-1]
            blocks.append({
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "⚠️ Report truncated due to size limits. Full report available on request."
                    }
                ]
            })
            
        logger.info(f"Created {len(blocks)} Slack blocks for response")
        return blocks
    
    async def generate_all_insights(self, command_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Generate insights from all data sources
        
        Args:
            command_data: Dict with command data from Slack
            
        Returns:
            Dict with response data for Slack
        """
        try:
            # Use hard-coded parameters
            channel_id = self.hard_coded_params["CHANNEL_ID_2"]
            jira_project = self.hard_coded_params["JIRA_PROJECT"]
            google_sheets_id = self.hard_coded_params["GOOGLE_SHEETS_ID"]
            google_sheet_name = self.hard_coded_params["GOOGLE_SHEET_NAME"]
            google_sheet_range = self.hard_coded_params["GOOGLE_SHEET_RANGE"]
            google_doc_id = self.hard_coded_params["GOOGLE_DOC_ID"]
            
            logger.info("Using the following parameters for data sources:")
            logger.info(f"Slack channel ID: {channel_id}")
            logger.info(f"JIRA project: {jira_project}")
            logger.info(f"Google Sheets ID: {google_sheets_id}")
            logger.info(f"Google Sheet name: {google_sheet_name}")
            logger.info(f"Google Sheet range: {google_sheet_range}")
            logger.info(f"Google Doc ID: {google_doc_id}")
            
            # Fetch data from all sources
            logger.info("Starting to fetch data from all sources...")
            
            # Use ThreadPoolExecutor for fetching Slack and JIRA data
            slack_messages = ""
            jira_summary = ""
            
            try:
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    # Start Slack and JIRA fetch operations
                    logger.info("Fetching Slack messages...")
                    slack_future = executor.submit(fetch_slack_messages, channel_id)
                    
                    logger.info("Fetching JIRA project data...")
                    jira_future = executor.submit(fetch_jira_project, jira_project)
                
                    # Get results (will block until complete)
                    slack_messages = slack_future.result()
                    logger.info("Successfully fetched Slack messages")
                    
                    jira_summary = jira_future.result()
                    logger.info("Successfully fetched JIRA data")
            except Exception as e:
                logger.error(f"Error fetching Slack or JIRA data: {str(e)}")
                logger.error(traceback.format_exc())
                if not slack_messages:
                    slack_messages = f"Error fetching Slack messages: {str(e)}"
                if not jira_summary:
                    jira_summary = f"Error fetching JIRA data: {str(e)}"
            
            # Fetch Google Docs data using the GoogleDocsDataSource
            final_summary_doc = ""
            try:
                logger.info("Fetching Google Docs data...")
                google_docs_input = {"document_urls": google_doc_id}
                google_docs_data = await self.google_docs_source.fetch_data(google_docs_input)
                final_summary_doc = self.google_docs_source.format_for_llm(google_docs_data)
                logger.info("Successfully fetched Google Docs data")
            except Exception as e:
                logger.error(f"Error fetching Google Docs data: {str(e)}")
                logger.error(traceback.format_exc())
                final_summary_doc = f"Error fetching Google Docs data: {str(e)}"
            
            # Fetch Google Sheets data using the GoogleSheetsDataSource
            sheets_data = ""
            try:
                logger.info("Fetching Google Sheets data...")
                google_sheets_input = {"spreadsheet_urls": google_sheets_id}
                google_sheets_data = await self.google_sheets_source.fetch_data(google_sheets_input)
                sheets_data = self.google_sheets_source.format_for_llm(google_sheets_data)
                logger.info("Successfully fetched Google Sheets data")
            except Exception as e:
                logger.error(f"Error fetching Google Sheets data: {str(e)}")
                logger.error(traceback.format_exc())
                sheets_data = f"Error fetching Google Sheets data: {str(e)}"
            
            # Check if any data sources returned errors
            data_source_errors = []
            if slack_messages.startswith("Error:"):
                data_source_errors.append(f"Slack: {slack_messages}")
            if sheets_data.startswith("Error:"):
                data_source_errors.append(f"Google Sheets: {sheets_data}")
            if final_summary_doc.startswith("Error:"):
                data_source_errors.append(f"Google Docs: {final_summary_doc}")
            if jira_summary.startswith("Error:"):
                data_source_errors.append(f"JIRA: {jira_summary}")
            
            if data_source_errors:
                logger.warning(f"Errors occurred while fetching data from some sources: {', '.join(data_source_errors)}")
            
            # Generate insights about GC Issuing from Slack
            logger.info("Generating Slack summary...")
            slack_prompt = f"""Tell me about updates from slack message added below. Concentrate only on "GC Issuing"
                {slack_messages}"""
            slack_summary = generate_ai_analysis(slack_prompt)
            
            # Generate sheets summary
            logger.info("Generating Sheets summary...")
            sheets_prompt = f"""
            Analyze the Google Sheet data, specifically filtering for metrics under the 'GC Issuance' category (L0 and associated L1s). Exclude any unrelated sections.
            For the filtered data:

            Compare the 'Target' and 'Actual' values — highlight significant gaps or overachievements.
            Identify metrics that are closest to their targets and those that are farthest.
            Analyze patterns or trends among the L1 metrics related to 'GC Issuance.'
            Suggest potential areas for improvement based on discrepancies between targets and actuals.
            Provide data-driven insights or recommendations to optimize performance in areas with notable gaps.

            Ensure the analysis remains focused only on 'GC Issuance' and avoids unrelated data." {sheets_data}"""
            sheets_summary = generate_ai_analysis(sheets_prompt)
            
            logger.info("Summaries generated")
            
            # Define summary sections
            summary_parts = [
                "📌 Executive Summary\nIn [Quarter], our primary focus is to:",
                "🎯 OKR Table\n| OKR | [Month] | [Previous Month] |",
                "🏆 Highlights\n1. **[Key achievement 1]** - [Brief explanation of impact].",
                "⚠️ Lowlights\n1. **[Challenge/blocker 1]** - [Brief explanation of impact].",
                "📌 Key Projects\n| Related OKR | Project | RAG Status | Status | Goal |",
                "🌍 GTM Updates\n### **[Initiative 1] Updates**",
                "🚧 Challenges\n### **[Issue Category 1]**\n- **Description:** [Brief description of the challenge]",
                "✅ Path to Green\n[OKR/Initiative 1]\n- Current status: [Current status of the OKR/initiative]"
            ]
            
            # Generate insights for each section
            logger.info("Generating final insights...")
            
            # Create section prompts and generate insights sequentially
            insights = []
            for part in summary_parts:
                # Limit the doc summary to 500 chars to avoid token limits
                doc_summary = final_summary_doc[:500] if len(final_summary_doc) > 500 else final_summary_doc
                
                prompt = f"""
                    Using this section template: {part}
                    Consolidate the following summaries into a well-formatted Slack message. Mark sure to put only relevant messages as per the section heading
                    Format the message with:
                    - Proper Slack markdown (bold, italics, bullet points)
                    - Clear section headers with emojis
                    - Concise, scannable content with no more than 1500 characters total
                    - A consistent and professional tone

                    Source summaries to consolidate:
                    - Slack: {slack_summary}
                    - Google Sheets: {sheets_summary}
                    - JIRA: {jira_summary}
                    - Google Doc: {doc_summary}..."""
                
                # Generate insight for this section with length guidance
                section_insight = generate_ai_analysis(prompt)
                
                # Add character count limit guidance for the LLM and retry with stricter limits
                if len(section_insight) > SLACK_MAX_TEXT_LENGTH:
                    logger.warning(f"Section insight too long ({len(section_insight)} chars), regenerating with stricter limits")
                    prompt += f"\n\nIMPORTANT: Keep your response under 1500 characters due to Slack message size limits."
                    section_insight = generate_ai_analysis(prompt)
                    
                    # If still too long, force truncate
                    if len(section_insight) > SLACK_MAX_TEXT_LENGTH:
                        logger.warning(f"Section still too long after regeneration ({len(section_insight)} chars), truncating")
                        section_insight = section_insight[:SLACK_MAX_TEXT_LENGTH - 100] + "... (content truncated due to length)"
                
                insights.append(section_insight)
                
            # Log the lengths of each insight for debugging
            for i, insight in enumerate(insights):
                logger.info(f"Insight {i+1} length: {len(insight)} characters")
            
            # Combine all insights into the final report
            final_insight = "\n".join(insights)
            
            # Format the response for Slack using our chunking function
            blocks = self._format_blocks_for_slack(insights)
            
            # Get the channel where the command was issued
            channel_id = command_data.get("channel_id")
            logger.info(f"Command was issued in channel_id: {channel_id}")
            
            # Make sure we explicitly include the channel ID in the response
            return {
                "response_type": "in_channel",  # Force response to be public in channel
                "channel": channel_id,          # Explicitly use the channel where command was issued
                "thread_ts": command_data.get("thread_ts"),  # Reply in thread if command was in thread
                "blocks": blocks
            }
            
        except Exception as e:
            logger.error(f"Error generating insights: {str(e)}")
            logger.error(traceback.format_exc())
            return {"text": f"Error generating Apollo insights: {str(e)}"} 