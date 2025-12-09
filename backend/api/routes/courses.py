from fastapi import APIRouter, HTTPException, Query
from typing import Optional, List
from pydantic import BaseModel

from api.utils.supabase_client import search_courses, get_supabase

router = APIRouter()

class Course(BaseModel):
    id: int
    subject: str
    catalog: str
    title: str
    average: Optional[float]
    instructor: Optional[str]

@router.get("/search")
async def search(
    query: Optional[str] = None,
    subject: Optional[str] = None,
    limit: int = Query(default=50, le=200)
):
    """Search for courses"""
    try:
        courses = await search_courses(query=query, subject=subject, limit=limit)
        return {"courses": courses, "count": len(courses)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/{subject}/{catalog}")
async def get_course_details(subject: str, catalog: str):
    """Get detailed info for a specific course"""
    try:
        supabase = get_supabase()
        response = supabase.table('courses')\
            .select('*')\
            .eq('subject', subject)\
            .eq('catalog', catalog)\
            .execute()
        
        if not response.data:
            raise HTTPException(status_code=404, detail="Course not found")
        
        # Calculate statistics across all sections
        courses = response.data
        avg_grade = sum(c['average'] for c in courses if c['average']) / len(courses)
        
        return {
            "course": {
                "subject": subject,
                "catalog": catalog,
                "title": courses[0]['title'],
                "average_grade": round(avg_grade, 2),
                "sections": courses
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/subjects")
async def get_subjects():
    """Get list of all subjects"""
    try:
        supabase = get_supabase()
        response = supabase.table('courses')\
            .select('subject')\
            .execute()
        
        subjects = list(set(course['subject'] for course in response.data))
        subjects.sort()
        
        return {"subjects": subjects}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))