from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from contextlib import asynccontextmanager
import time

from .config import settings, get_settings
from .logging_config import setup_logging
from .exceptions import (
    AppException,
    app_exception_handler,
    validation_exception_handler,
    general_exception_handler
)

# Import routes
from api.routes import chat, courses, users

# Setup logging
logger = setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events"""
    # Startup
    logger.info(f"🚀 Starting {settings.API_TITLE} v{settings.API_VERSION}")
    logger.info(f"Environment: {settings.ENVIRONMENT}")
    logger.info(f"Debug mode: {settings.DEBUG}")
    
    yield
    
    # Shutdown
    logger.info("👋 Shutting down gracefully...")


app = FastAPI(
    title=settings.API_TITLE,
    description="AI-powered course recommendation system for McGill University",
    version=settings.API_VERSION,
    lifespan=lifespan,
    docs_url="/api/docs" if settings.DEBUG else None,  # Disable docs in production
    redoc_url="/api/redoc" if settings.DEBUG else None,
)

# Get origins from settings
origins = settings.ALLOWED_ORIGINS

# Add any additional preview deployments if needed
if settings.ENVIRONMENT == "production":
    # You can add specific preview URLs here if needed
    pass

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,  # Use explicit list from config
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)

# Request timing middleware
@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    """Add request timing and ID"""
    start_time = time.time()
    request_id = f"{int(start_time * 1000)}"
    
    response = await call_next(request)
    
    process_time = time.time() - start_time
    response.headers["X-Process-Time"] = str(process_time)
    response.headers["X-Request-ID"] = request_id
    
    return response

# Register exception handlers
app.add_exception_handler(AppException, app_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, general_exception_handler)

# Include routers
app.include_router(chat.router, prefix=f"{settings.API_PREFIX}/chat", tags=["Chat"])
app.include_router(courses.router, prefix=f"{settings.API_PREFIX}/courses", tags=["Courses"])
app.include_router(users.router, prefix=f"{settings.API_PREFIX}/users", tags=["Users"])


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "service": settings.API_TITLE,
        "version": settings.API_VERSION,
        "status": "operational",
        "docs": f"{settings.API_PREFIX}/docs" if settings.DEBUG else None
    }


@app.get(f"{settings.API_PREFIX}/health")
async def health_check():
    """Detailed health check"""
    return {
        "status": "healthy",
        "version": settings.API_VERSION,
        "environment": settings.ENVIRONMENT,
        "services": {
            "database": "connected",  # Could add actual DB health check
            "ai": "configured"
        }
    }