from typing import Dict, Any, Optional
from ..data_sources.base import DataSourceRegistry
import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

class FlowState:
    """Represents the state of a user's flow"""
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.selected_sources: list = []
        self.source_details: Dict[str, Any] = {}
        self.prompt: Optional[str] = None
        self.step = 1
        self.last_updated = datetime.now()
        
    def to_dict(self) -> Dict[str, Any]:
        return {
            'user_id': self.user_id,
            'selected_sources': self.selected_sources,
            'source_details': self.source_details,
            'prompt': self.prompt,
            'step': self.step,
            'last_updated': self.last_updated.isoformat()
        }
        
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'FlowState':
        state = cls(data['user_id'])
        state.selected_sources = data['selected_sources']
        state.source_details = data['source_details']
        state.prompt = data['prompt']
        state.step = data['step']
        state.last_updated = datetime.fromisoformat(data['last_updated'])
        return state

class FlowController:
    """Controls the flow of the interaction"""
    
    def __init__(self, source_registry: DataSourceRegistry):
        self.source_registry = source_registry
        self._states: Dict[str, FlowState] = {}
        
    def get_or_create_state(self, user_id: str) -> FlowState:
        """Get existing state or create new one"""
        if user_id not in self._states:
            self._states[user_id] = FlowState(user_id)
        return self._states[user_id]
        
    def clean_old_states(self, max_age_minutes: int = 30):
        """Clean up old state data"""
        now = datetime.now()
        expired = [
            user_id for user_id, state in self._states.items()
            if (now - state.last_updated) > timedelta(minutes=max_age_minutes)
        ]
        for user_id in expired:
            del self._states[user_id]
    
    def get_source_selection_view(self) -> Dict[str, Any]:
        """Get the source selection view"""
        return {
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "Select your data sources:"
                    }
                },
                {
                    "type": "actions",
                    "block_id": "source_selection",
                    "elements": [
                        {
                            "type": "checkboxes",
                            "action_id": "select_sources",
                            "options": [
                                {
                                    "text": {"type": "plain_text", "text": source, "emoji": True},
                                    "value": source
                                }
                                for source in self.source_registry.get_sources()
                            ]
                        }
                    ]
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Next", "emoji": True},
                            "action_id": "next_step"
                        }
                    ]
                }
            ]
        }
        
    def get_source_form(self, source_name: str) -> Dict[str, Any]:
        """Get the form for a specific source"""
        source = self.source_registry.get_source(source_name)
        if not source:
            return {"text": "Invalid source"}
            
        form_fields = source.get_form_fields()
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"Configure {source_name}:"
                }
            }
        ]
        
        for field in form_fields:
            blocks.append({
                "type": "input",
                "block_id": f"{field['name']}_block",
                "element": {
                    "type": "plain_text_input",
                    "action_id": field["name"],
                    "placeholder": {
                        "type": "plain_text",
                        "text": field.get("placeholder", "Enter value")
                    }
                },
                "label": {
                    "type": "plain_text",
                    "text": field["label"]
                },
                "optional": not field.get("required", True)
            })
            
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Submit", "emoji": True},
                    "action_id": "submit_source_form"
                }
            ]
        })
        
        return {"blocks": blocks}
        
    def get_prompt_input_view(self) -> Dict[str, Any]:
        """Get the prompt input view"""
        return {
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "Enter your analysis prompt:"
                    }
                },
                {
                    "type": "input",
                    "block_id": "prompt_block",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "prompt",
                        "multiline": True
                    },
                    "label": {
                        "type": "plain_text",
                        "text": "What would you like to analyze?"
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Analyze", "emoji": True},
                            "action_id": "submit_prompt"
                        }
                    ]
                }
            ]
        }
    
    def get_initial_message(self) -> Dict[str, Any]:
        """Get the initial source selection message"""
        sources = self.source_registry.get_all_sources()
        return {
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "Select your data sources:"
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "checkboxes",
                            "options": [
                                {
                                    "text": {
                                        "type": "plain_text",
                                        "text": source
                                    },
                                    "value": source
                                }
                                for source in sources
                            ],
                            "action_id": "select_sources"
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
                                "text": "Next"
                            },
                            "action_id": "next_step"
                        }
                    ]
                }
            ]
        }
    
    def get_source_details_message(self, state: FlowState) -> Dict[str, Any]:
        """Get the source details form"""
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "Enter details for each selected source:"
                }
            }
        ]
        
        for source in state.selected_sources:
            source_obj = self.source_registry.get_source(source)
            if source_obj:
                form_fields = source_obj.get_form_fields()
                blocks.extend(self._create_source_fields(source, form_fields))
                
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Back"
                    },
                    "action_id": "previous_step"
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Next"
                    },
                    "action_id": "next_step"
                }
            ]
        })
        
        return {"blocks": blocks}
    
    def get_prompt_message(self) -> Dict[str, Any]:
        """Get the prompt input message"""
        return {
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "Enter your analysis prompt:"
                    }
                },
                {
                    "type": "input",
                    "element": {
                        "type": "plain_text_input",
                        "multiline": True,
                        "action_id": "prompt_input"
                    },
                    "label": {
                        "type": "plain_text",
                        "text": "Prompt"
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Back"
                            },
                            "action_id": "previous_step"
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Submit"
                            },
                            "action_id": "submit"
                        }
                    ]
                }
            ]
        }
    
    def _create_source_fields(self, source: str, form_fields: Dict[str, Any]) -> list:
        """Create form fields for a source"""
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{source}*"
                }
            }
        ]
        
        for field in form_fields['fields']:
            blocks.append({
                "type": "input",
                "element": {
                    "type": "plain_text_input",
                    "action_id": f"{source}_{field['name']}",
                    "placeholder": {
                        "type": "plain_text",
                        "text": field['placeholder']
                    }
                },
                "label": {
                    "type": "plain_text",
                    "text": field['label']
                },
                "optional": field.get('optional', False)
            })
            
            if field.get('add_more', False):
                blocks.append({
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Add Another"
                            },
                            "action_id": f"add_{source}_{field['name']}"
                        }
                    ]
                })
        
        return blocks 