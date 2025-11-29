from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from mcgill_advisor import McGillAdvisorAI
import anthropic
import os
from dotenv import load_dotenv
from pathlib import Path

# --- FIX 1: Explicitly load .env from the current directory (backend/) ---
# This ensures it works regardless of where you run the terminal command from
current_dir = Path(__file__).resolve().parent
env_path = current_dir / '.env'
load_dotenv(dotenv_path=env_path)

api_key = os.environ.get("ANTHROPIC_API_KEY")

# Initialize App & Clients
app = FastAPI()
advisor = McGillAdvisorAI(str(current_dir / "ClassAverageCrowdSourcing.csv"))
client = anthropic.AsyncAnthropic(api_key=api_key)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ChatRequest(BaseModel):
    message: str
    history: list = []

class PredictionRequest(BaseModel):
    course_code: str
    student_gpa: float

@app.post("/chat")
async def chat_endpoint(request: ChatRequest):
    try:
        # Construct system prompt
        system_prompt = """You are a helpful McGill University academic advisor. You're friendly and conversational.

        Your capabilities:
        - Course recommendations based on GPA, major, interests
        - Grade predictions using historical data
        - Career advice
        - Academic planning help

        Style:
        - Be conversational and natural
        - Use emojis occasionally
        - Be encouraging and supportive
        - If you don't have specific data, be honest

        When students ask about courses, use the data provided in the context."""
        
        # --- FIX 2: Restore the Multi-Model Fallback Loop ---
        # This matches the original mcgill_chatbot.py logic.
        # It tries the newest models first. If your key gets a 404 on one,
        # it catches the error and immediately tries the next one.
        models_to_try = [
            "claude-3-5-sonnet-20241022",
            "claude-3-5-sonnet-20240620",
            "claude-3-opus-20240229",
            "claude-3-sonnet-20240229",
            "claude-3-haiku-20240307" # Added as a final fallback
        ]
        
        last_error = None
        
        for model in models_to_try:
            try:
                print(f"🤖 Attempting with model: {model}...")
                response = await client.messages.create(
                    model=model,
                    max_tokens=800,
                    system=system_prompt,
                    messages=[{"role": "user", "content": request.message}]
                )
                # If successful, return immediately
                print(f"✅ Success with {model}!")
                return {"response": response.content[0].text}
                
            except anthropic.NotFoundError:
                # This is the 404 error you were seeing.
                # We simply ignore it and let the loop continue to the next model.
                print(f"⚠️  Model {model} not available on this key (404). Trying next...")
                last_error = f"Model {model} not found."
                continue
                
            except anthropic.APIError as e:
                # Other API errors (like credit limits) might stop us
                print(f"❌ API Error on {model}: {e}")
                last_error = str(e)
                continue

        # If we loop through ALL models and fail every time:
        raise HTTPException(status_code=500, detail=f"Failed to connect to any Claude model. Last error: {last_error}")

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/predict")
def predict_grade(request: PredictionRequest):
    # Ensure profile exists
    if not advisor.student_profile:
        advisor.student_profile = {}
        
    advisor.student_profile['current_gpa'] = request.student_gpa
    advisor.student_profile.setdefault('difficulty_preference', 2)
    
    grade, conf = advisor.predict_student_grade(request.course_code)
    
    return {
        "course": request.course_code,
        "predicted_grade": grade,
        "confidence": conf
    }