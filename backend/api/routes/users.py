"""
User management endpoints with improved error handling
"""
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, EmailStr, Field, validator, field_validator
from typing import Optional
import logging

from api.utils.supabase_client import (
    get_user_by_id,
    get_user_by_email,
    create_user as create_user_db,
    update_user as update_user_db
)
from api.exceptions import (
    UserNotFoundException,
    UserAlreadyExistsException,
    DatabaseException
)
from ..config import settings

router = APIRouter()
logger = logging.getLogger(__name__)


class UserCreate(BaseModel):
    """User creation schema"""
    id: str = Field(..., description="Supabase Auth user ID")
    email: EmailStr
    username: Optional[str] = Field(None, min_length=3, max_length=20)
    major: Optional[str] = Field(None, max_length=100)
    year: Optional[int] = Field(None, ge=1, le=10)
    interests: Optional[str] = Field(None, max_length=500)
    current_gpa: Optional[float] = Field(None, ge=0.0, le=4.0)
    
    @validator('username')
    def validate_username(cls, v):
        if v and not v.replace('_', '').isalnum():
            raise ValueError('Username must contain only letters, numbers, and underscores')
        return v
    
    class Config:
        schema_extra = {
            "example": {
                "id": "123e4567-e89b-12d3-a456-426614174000",
                "email": "student@mail.mcgill.ca",
                "username": "mcgill_student",
                "major": "Computer Science",
                "year": 3,
                "interests": "Machine Learning, Web Development",
                "current_gpa": 3.5
            }
        }


class UserUpdate(BaseModel):
    """User update schema"""
    username: Optional[str] = Field(None, min_length=3, max_length=20)
    major: Optional[str] = Field(None, max_length=100)
    year: Optional[int] = Field(None, ge=1, le=10)
    interests: Optional[str] = Field(None, max_length=500)
    current_gpa: Optional[float] = Field(None, ge=0.0, le=4.0)
    
    @field_validator('username')
    @classmethod
    def validate_username(cls, v):
        if v and not v.replace('_', '').isalnum():
            raise ValueError('Username must contain only letters, numbers, and underscores')
        return v
    
    @field_validator('major', 'interests', 'username')
    @classmethod
    def strip_empty_strings(cls, v):
        """Convert empty strings to None"""
        if v is not None and isinstance(v, str) and not v.strip():
            return None
        return v if v is None else v.strip()
    
    @field_validator('year')
    @classmethod
    def validate_year(cls, v):
        """Convert empty string or 0 to None"""
        if v == 0 or v == '':
            return None
        return v
    
    @field_validator('current_gpa')
    @classmethod
    def validate_gpa(cls, v):
        """Convert empty string to None"""
        if v == '' or v is None:
            return None
        return v

class UserResponse(BaseModel):
    """User response schema"""
    id: str
    email: str
    username: Optional[str]
    major: Optional[str]
    year: Optional[int]
    interests: Optional[str]
    current_gpa: Optional[float]
    created_at: Optional[str]


@router.post("/", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_new_user(user: UserCreate):
    """Create a new user profile"""
    try:
        logger.info(f"=== CREATE USER REQUEST ===")
        logger.info(f"User ID: {user.id}")
        logger.info(f"Email: {user.email}")
        logger.info(f"Username: {user.username}")
        
        # Check if profile exists by ID (not email!)
        try:
            existing = get_user_by_id(user.id)
            logger.warning(f"User profile already exists: {user.id}")
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "user_already_exists",
                    "message": "User profile already exists for this ID"
                }
            )
        except UserNotFoundException:
            logger.info(f"User profile doesn't exist yet, creating...")
            pass
        
        # Create user
        user_data = user.dict(exclude_none=True)
        created_user = create_user_db(user_data)
        
        logger.info(f"✓ User profile created successfully: {created_user['id']}")
        return {
            "user": created_user,
            "message": "User profile created successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"✗ Unexpected error creating user")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "internal_error",
                "message": "Failed to create user profile"
            }
        )


@router.get("/{user_id}", response_model=dict)
async def get_user(user_id: str):
    """
    Get user profile by ID
    
    - **user_id**: The user's unique identifier
    """
    try:
        user = get_user_by_id(user_id)
        return {"user": user}
    except UserNotFoundException as e:
        logger.warning(f"User not found: {user_id}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "user_not_found",
                "message": "User profile not found"
            }
        )
    except DatabaseException:
        raise
    except Exception as e:
        logger.exception(f"Unexpected error getting user: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while retrieving user profile"
        )


@router.patch("/{user_id}", response_model=dict)
async def update_user(user_id: str, updates: UserUpdate):
    """
    Update user profile
    
    - **user_id**: The user's unique identifier
    - Updates can include: username, major, year, interests, current_gpa
    """
    try:
        # Verify user exists first
        get_user_by_id(user_id)
        
        # Update user
        update_data = updates.dict(exclude_none=True)
        
        if not update_data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No valid fields provided for update"
            )
        
        updated_user = update_user_db(user_id, update_data)
        
        logger.info(f"User updated successfully: {user_id}")
        return {
            "user": updated_user,
            "message": "User profile updated successfully"
        }
        
    except UserNotFoundException:
        raise
    except DatabaseException:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Unexpected error updating user: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while updating user profile"
        )


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: str):
    """
    Delete user profile (soft delete recommended in production)
    
    - **user_id**: The user's unique identifier
    """
    # TODO: Implement soft delete or actual deletion
    # For now, return not implemented
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="User deletion not yet implemented"
    )