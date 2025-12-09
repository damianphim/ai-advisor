from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from typing import List, Optional
import anthropic
import os

from api.utils.supabase_client import (
    get_user_by_id,
    get_chat_history,
    save_message,
    search_courses
)

router = APIRouter()

class ChatRequest(BaseModel):
    message: str
    user_id: str

class ChatResponse(BaseModel):
    response: str
    user_id: str

@router.post("/send", response_model=ChatResponse)
async def send_message(request: ChatRequest):
    """Send a message and get AI response"""
    try:
        # Verify user exists
        user = await get_user_by_id(request.user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Save user message
        await save_message(request.user_id, "user", request.message)
        
        # Get chat history for context
        history = await get_chat_history(request.user_id, limit=10)
        
        # Build context for Claude
        context = build_context(user, history)
        
        # Call Claude API
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        
        message = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=1024,
            system=context,
            messages=[
                {"role": msg["role"], "content": msg["content"]}
                for msg in history[-6:]  # Last 3 exchanges
            ] + [
                {"role": "user", "content": request.message}
            ]
        )
        
        assistant_response = message.content[0].text
        
        # Save assistant response
        await save_message(request.user_id, "assistant", assistant_response)
        
        return ChatResponse(
            response=assistant_response,
            user_id=request.user_id
        )
        
    except Exception as e:
        print(f"Error in chat: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

def build_context(user: dict, history: list) -> str:
    """Build system context for Claude"""
    return f"""You are an AI academic advisor for McGill University students.

Student Profile:
- Major: {user.get('major', 'Undeclared')}
- Year: {user.get('year', 'Not specified')}
- Interests: {user.get('interests', 'Not specified')}
- Current GPA: {user.get('current_gpa', 'Not specified')}

Your role is to:
1. Provide personalized course recommendations
2. Help with course selection and planning
3. Answer questions about prerequisites and requirements
4. Offer study advice and academic guidance
5. Predict potential grades based on historical data

Be friendly, encouraging, and specific. Use data when available.
Keep responses concise but informative."""

@router.get("/history/{user_id}")
async def get_history(user_id: str, limit: int = 50):
    """Get user's chat history"""
    try:
        history = await get_chat_history(user_id, limit)
        return {"messages": history}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))