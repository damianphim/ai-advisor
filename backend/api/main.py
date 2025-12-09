from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import os
from dotenv import load_dotenv

load_dotenv()

# Import routes
from api.routes import chat, courses, users

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events"""
    # Startup
    print("🚀 Starting McGill AI Advisor API...")
    
    # Verify environment variables
    required_vars = ["SUPABASE_URL", "SUPABASE_SERVICE_KEY", "ANTHROPIC_API_KEY"]
    missing_vars = [var for var in required_vars if not os.environ.get(var)]
    
    if missing_vars:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing_vars)}")
    
    print("✅ Environment variables configured")
    
    yield
    
    # Shutdown
    print("👋 Shutting down...")

app = FastAPI(
    title="McGill AI Advisor API",
    description="AI-powered course recommendation system",
    version="2.0.0",
    lifespan=lifespan
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",  # Local development
        "https://your-frontend.vercel.app",  # Production frontend
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(chat.router, prefix="/api/chat", tags=["Chat"])
app.include_router(courses.router, prefix="/api/courses", tags=["Courses"])
app.include_router(users.router, prefix="/api/users", tags=["Users"])

@app.get("/")
async def root():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "message": "McGill AI Advisor API",
        "version": "2.0.0"
    }

@app.get("/api/health")
async def health_check():
    """Detailed health check"""
    return {
        "status": "healthy",
        "supabase": "connected" if os.environ.get("SUPABASE_URL") else "not configured",
        "claude": "configured" if os.environ.get("ANTHROPIC_API_KEY") else "not configured"
    }