from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr
from typing import Optional

from api.utils.supabase_client import (
    get_user_by_id,
    get_user_by_email,
    create_user,
    get_supabase
)

router = APIRouter()

class UserCreate(BaseModel):
    email: EmailStr
    username: Optional[str] = None
    major: Optional[str] = None
    year: Optional[int] = None
    interests: Optional[str] = None
    current_gpa: Optional[float] = None

class UserUpdate(BaseModel):
    major: Optional[str] = None
    year: Optional[int] = None
    interests: Optional[str] = None
    current_gpa: Optional[float] = None

@router.post("/")
async def create_new_user(user: UserCreate):
    """Create a new user"""
    try:
        # Check if user already exists
        try:
            existing = await get_user_by_email(user.email)
            if existing:
                raise HTTPException(status_code=400, detail="User already exists")
        except:
            pass  # User doesn't exist, continue
        
        new_user = await create_user(**user.dict())
        return {"user": new_user}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/{user_id}")
async def get_user(user_id: str):
    """Get user profile"""
    try:
        user = await get_user_by_id(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return {"user": user}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.patch("/{user_id}")
async def update_user(user_id: str, updates: UserUpdate):
    """Update user profile"""
    try:
        supabase = get_supabase()
        
        # Filter out None values
        update_data = {k: v for k, v in updates.dict().items() if v is not None}
        
        if not update_data:
            raise HTTPException(status_code=400, detail="No updates provided")
        
        response = supabase.table('users')\
            .update(update_data)\
            .eq('id', user_id)\
            .execute()
        
        if not response.data:
            raise HTTPException(status_code=404, detail="User not found")
        
        return {"user": response.data[0]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))