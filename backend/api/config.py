"""
Centralized configuration management with validation
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator
from typing import List
import os


class Settings(BaseSettings):
    """Application settings with validation"""
    
    # API Configuration
    API_TITLE: str = "McGill AI Advisor API"
    API_VERSION: str = "3.0.0"
    API_PREFIX: str = "/api"
    
    # Environment
    ENVIRONMENT: str = "development"
    DEBUG: bool = False
    
    # Database
    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: str
    
    # AI
    ANTHROPIC_API_KEY: str
    CLAUDE_MODEL: str = "claude-3-5-sonnet-20241022"
    CLAUDE_MAX_TOKENS: int = 2048
    
    # Chat Configuration
    CHAT_HISTORY_LIMIT: int = 10
    CHAT_CONTEXT_MESSAGES: int = 6
    MAX_MESSAGE_LENGTH: int = 4000
    
    # Course Search Configuration
    DEFAULT_SEARCH_LIMIT: int = 50
    MAX_SEARCH_LIMIT: int = 200
    
    # Security - Load from environment
    ALLOWED_ORIGINS: List[str] = Field(default=[
        "http://localhost:5173",
        "https://ai-advisor-pi.vercel.app"
    ])

    @field_validator('ALLOWED_ORIGINS', mode='before')
    @classmethod
    def parse_origins(cls, v):
        if isinstance(v, str):
            # Parse comma-separated string from env
            return [origin.strip() for origin in v.split(',')]
    return v
    
    # Rate Limiting
    RATE_LIMIT_PER_MINUTE: int = 20
    CHAT_RATE_LIMIT_PER_MINUTE: int = 10
    
    # Timeouts (seconds)
    REQUEST_TIMEOUT: int = 30
    DATABASE_TIMEOUT: int = 10
    
    @field_validator('ENVIRONMENT')
    @classmethod
    def validate_environment(cls, v: str) -> str:
        allowed = ['development', 'staging', 'production']
        if v not in allowed:
            raise ValueError(f'ENVIRONMENT must be one of {allowed}')
        return v
    
    @field_validator('SUPABASE_URL')
    @classmethod
    def validate_supabase_url(cls, v: str) -> str:
        if not v.startswith('https://'):
            raise ValueError('SUPABASE_URL must start with https://')
        return v
    
    @field_validator('ANTHROPIC_API_KEY')
    @classmethod
    def validate_anthropic_key(cls, v: str) -> str:
        if not v.startswith('sk-ant-'):
            raise ValueError('Invalid ANTHROPIC_API_KEY format')
        return v
    
    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra='ignore'
    )


# Global settings instance
settings = Settings()


def get_settings() -> Settings:
    """Get application settings"""
    return settings