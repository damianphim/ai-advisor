"""
McGill Course Advisory AI - Logic Module
Refactored for Backend API usage.
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Tuple, Optional
import json
import os
import logging
from dataclasses import dataclass, asdict
from enum import Enum

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class DifficultyLevel(Enum):
    EASY = 1
    MODERATE = 2  
    CHALLENGING = 3
    VERY_HARD = 4

@dataclass
class CourseRecommendation:
    course_code: str
    course_name: str
    predicted_grade: float
    difficulty_score: float
    class_average: float
    credits: int
    reasons: List[str]
    warnings: List[str]
    confidence: float
    term_offered: str = "Unknown"

class McGillAdvisorAI:
    """
    Main advisory AI logic.
    """
    
    def __init__(self, csv_file: str = "ClassAverageCrowdSourcing.csv"):
        self.df = None
        self.student_profile = {}
        self.load_grade_data(csv_file)
        
    def load_grade_data(self, csv_file: str):
        """Load data from your CSV file with robust error handling"""
        try:
            if os.path.exists(csv_file):
                logger.info(f"Attempting to load {csv_file}...")
                
                # Try different parsing strategies
                parsing_strategies = [
                    {'sep': ',', 'quoting': 0},
                    {'sep': ',', 'quoting': 1, 'skipinitialspace': True},
                    {'sep': ',', 'quoting': 1, 'skipinitialspace': True, 'on_bad_lines': 'skip'},
                    {'sep': ';', 'quoting': 1},
                    {'sep': '\t', 'quoting': 1}
                ]
                
                for i, strategy in enumerate(parsing_strategies):
                    try:
                        self.df = pd.read_csv(csv_file, **strategy)
                        logger.info(f"Strategy {i+1} worked! Loaded {len(self.df)} rows.")
                        break
                    except Exception:
                        continue
                else:
                    # If all strategies fail, try manual line-by-line parsing
                    logger.warning("All standard strategies failed. Attempting manual parsing...")
                    self.df = self._manual_csv_parse(csv_file)
                
                if self.df is None or self.df.empty:
                    logger.error("Could not parse the CSV file with any method")
                    return
                
                # Identify the course column
                course_column = None
                possible_course_cols = ['Course', 'course', 'Class', 'class', 'Course Code', 'course_code']
                for col in possible_course_cols:
                    if col in self.df.columns:
                        course_column = col
                        break
                
                if course_column is None:
                    logger.error(f"Could not find a course code column. Available: {list(self.df.columns)}")
                    return
                
                # Clean the data
                self.df = self.df.dropna(subset=[course_column])
                self.df[course_column] = self.df[course_column].astype(str).str.upper().str.strip()
                
                # Standardize column name
                if course_column != 'Course':
                    self.df['Course'] = self.df[course_column]
                
                # Handle grade columns
                grade_columns = [col for col in self.df.columns if 'ave' in col.lower() or 'grade' in col.lower()]
                if grade_columns:
                    self.df['Class Ave'] = pd.to_numeric(self.df[grade_columns[0]], errors='coerce')
                
                # Handle credits column
                credit_columns = [col for col in self.df.columns if 'credit' in col.lower()]
                if credit_columns:
                    self.df['Credits'] = pd.to_numeric(self.df[credit_columns[0]], errors='coerce')
                
                # Remove clearly invalid courses
                self.df = self.df[~self.df['Course'].str.contains('#REF!|NaN|nan', case=False, na=False)]
                
                logger.info(f"Successfully cleaned data: {len(self.df)} course records")
                
            else:
                logger.error(f"File {csv_file} not found!")
                
        except Exception as e:
            logger.error(f"Error loading data: {e}")
    
    def _manual_csv_parse(self, csv_file: str):
        """Manual CSV parsing as fallback"""
        try:
            rows = []
            headers = None
            
            with open(csv_file, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    
                    fields = []
                    current_field = ""
                    in_quotes = False
                    
                    for char in line:
                        if char == '"':
                            in_quotes = not in_quotes
                        elif char == ',' and not in_quotes:
                            fields.append(current_field.strip().strip('"'))
                            current_field = ""
                        else:
                            current_field += char
                    fields.append(current_field.strip().strip('"'))
                    
                    if headers is None:
                        headers = fields
                    else:
                        while len(fields) < len(headers):
                            fields.append('')
                        fields = fields[:len(headers)]
                        rows.append(fields)
            
            if headers and rows:
                return pd.DataFrame(rows, columns=headers)
            return None
                
        except Exception as e:
            logger.error(f"Manual parsing failed: {e}")
            return None
    
    def create_student_profile(self, 
                             name: str,
                             major: str,
                             year: int,
                             completed_courses: List[str],
                             current_gpa: float,
                             target_gpa: float = None,
                             interests: List[str] = None,
                             career_goals: List[str] = None,
                             difficulty_preference: DifficultyLevel = DifficultyLevel.MODERATE,
                             credits_per_semester: int = 15) -> Dict:
        """Create a student profile for personalized recommendations"""
        
        completed_courses = [course.upper().strip() for course in completed_courses]
        interests = interests or []
        career_goals = career_goals or []
        target_gpa = target_gpa or current_gpa
        
        self.student_profile = {
            'name': name,
            'major': major,
            'year': year,
            'completed_courses': completed_courses,
            'current_gpa': current_gpa,
            'target_gpa': target_gpa,
            'interests': interests,
            'career_goals': career_goals,
            'difficulty_preference': difficulty_preference,
            'credits_per_semester': credits_per_semester,
            'strong_subjects': self._identify_strong_subjects(completed_courses),
            'weak_subjects': []
        }
        
        logger.info(f"Created profile for {name} ({major})")
        return self.student_profile
    
    def _identify_strong_subjects(self, completed_courses: List[str]) -> List[str]:
        """Identify subjects where student has taken multiple courses"""
        subjects = []
        for course in completed_courses:
            if len(course) >= 4:
                subject = course[:4]
                subjects.append(subject)
        
        if not subjects:
            return []
            
        subject_counts = pd.Series(subjects).value_counts()
        return subject_counts.head(3).index.tolist()
    
    def predict_student_grade(self, course_code: str) -> Tuple[float, float]:
        """
        Predict student's likely grade in a course
        Returns (predicted_grade, confidence_score)
        """
        if self.df is None or course_code.upper() not in self.df['Course'].values:
            return 0.0, 0.0
        
        course_data = self.df[self.df['Course'] == course_code.upper()]
        if 'Class Ave' not in course_data.columns:
            return 0.0, 0.0
            
        base_average = course_data['Class Ave'].mean()
        
        if pd.isna(base_average):
            return 0.0, 0.0
        
        predicted_grade = base_average
        confidence = 0.3
        
        if self.student_profile:
            gpa_adjustment = (self.student_profile.get('current_gpa', 3.0) - 2.7) * 0.3
            predicted_grade += gpa_adjustment
            confidence += 0.2
            
            course_subject = course_code[:4] if len(course_code) >= 4 else ""
            if course_subject in self.student_profile.get('strong_subjects', []):
                predicted_grade += 0.2
                confidence += 0.3
            
            course_difficulty = self.calculate_difficulty_score(course_code)
            pref_difficulty = self.student_profile.get('difficulty_preference', DifficultyLevel.MODERATE)
            if isinstance(pref_difficulty, DifficultyLevel):
                 pref_difficulty = pref_difficulty.value
            
            if abs(course_difficulty - pref_difficulty) <= 1:
                confidence += 0.2
            
        predicted_grade = max(0.0, min(4.0, predicted_grade))
        confidence = max(0.0, min(1.0, confidence))
        
        return predicted_grade, confidence
    
    def calculate_difficulty_score(self, course_code: str) -> float:
        """Calculate difficulty score (1-4) based on class averages"""
        if self.df is None:
            return 2.5
        
        course_data = self.df[self.df['Course'] == course_code.upper()]
        if course_data.empty or 'Class Ave' not in course_data.columns:
            return 2.5
        
        avg_grade = course_data['Class Ave'].mean()
        if pd.isna(avg_grade):
            return 2.5
        
        if avg_grade >= 3.7: return 1.5
        elif avg_grade >= 3.3: return 2.0
        elif avg_grade >= 3.0: return 2.5
        elif avg_grade >= 2.7: return 3.0
        else: return 3.5
    
    def get_course_recommendations(self, 
                                 num_courses: int = 8,
                                 exclude_completed: bool = True,
                                 min_credits: int = 3,
                                 subject_filter: List[str] = None) -> List[CourseRecommendation]:
        """Get personalized course recommendations"""
        
        if self.df is None:
            return []
        
        filtered_df = self.df.copy()
        
        if exclude_completed and self.student_profile:
            completed = self.student_profile.get('completed_courses', [])
            filtered_df = filtered_df[~filtered_df['Course'].isin(completed)]
        
        if subject_filter:
            subject_filter = [s.upper() for s in subject_filter]
            filtered_df = filtered_df[filtered_df['Course'].str[:4].isin(subject_filter)]
        
        if 'Credits' in filtered_df.columns:
            filtered_df = filtered_df[filtered_df['Credits'] >= min_credits]
        
        unique_courses = filtered_df.drop_duplicates('Course')
        recommendations = []
        
        for _, course_row in unique_courses.iterrows():
            course_code = course_row['Course']
            
            if pd.isna(course_code) or not isinstance(course_code, str):
                continue
            
            predicted_grade, confidence = self.predict_student_grade(course_code)
            difficulty = self.calculate_difficulty_score(course_code)
            
            class_avg = course_row.get('Class Ave', 0.0)
            credits_val = int(course_row.get('Credits', 3))
            term = course_row.get('Term Name', 'Unknown')
            
            reasons = self._generate_reasons(course_code, predicted_grade, difficulty, class_avg)
            warnings = self._generate_warnings(course_code, difficulty, predicted_grade)
            
            rec = CourseRecommendation(
                course_code=course_code,
                course_name=f"Course {course_code}",
                predicted_grade=predicted_grade,
                difficulty_score=difficulty,
                class_average=class_avg,
                credits=credits_val,
                reasons=reasons,
                warnings=warnings,
                confidence=confidence,
                term_offered=term
            )
            recommendations.append(rec)
        
        def score_recommendation(rec):
            base_score = rec.predicted_grade * rec.confidence
            difficulty_penalty = abs(2.5 - rec.difficulty_score) * 0.1
            return base_score - difficulty_penalty
        
        recommendations.sort(key=score_recommendation, reverse=True)
        return recommendations[:num_courses]
    
    def _generate_reasons(self, course_code: str, pred_grade: float, difficulty: float, class_avg: float) -> List[str]:
        """Generate reasons why this course is recommended"""
        reasons = []
        if pred_grade >= 3.5:
            reasons.append(f"High predicted grade ({pred_grade:.1f})")
        if class_avg >= 3.5:
            reasons.append(f"Historically high class average ({class_avg:.1f})")
        
        if self.student_profile:
            subject = course_code[:4] if len(course_code) >= 4 else ""
            if subject in self.student_profile.get('strong_subjects', []):
                reasons.append(f"Matches your strength in {subject}")
                
            pref_difficulty = self.student_profile.get('difficulty_preference', DifficultyLevel.MODERATE)
            if isinstance(pref_difficulty, DifficultyLevel):
                 pref_difficulty = pref_difficulty.value
                 
            if abs(difficulty - pref_difficulty) <= 0.5:
                reasons.append("Good difficulty match for your preferences")
        
        if difficulty <= 2.0:
            reasons.append("Manageable difficulty level")
        return reasons
    
    def _generate_warnings(self, course_code: str, difficulty: float, pred_grade: float) -> List[str]:
        """Generate warnings about potential challenges"""
        warnings = []
        if difficulty >= 3.2:
            warnings.append("High difficulty course")
        if pred_grade < 2.7:
            warnings.append("Below-average predicted performance")
        
        if self.student_profile:
            gpa = self.student_profile.get('current_gpa', 3.0)
            if gpa < 3.0 and difficulty > 3.0:
                warnings.append("May be challenging given current GPA")
        return warnings