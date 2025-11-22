#!/usr/bin/env python3
"""
McGill Course Advisory AI - Complete Application
Single file with everything you need
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Tuple, Optional
import json
import os
from dataclasses import dataclass, asdict
from enum import Enum
import sys

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
    Main advisory AI that works with your existing data structure
    """
    
    def __init__(self, csv_file: str = "ClassAverageCrowdSourcing.csv"):
        self.df = None
        self.student_profile = {}
        self.load_grade_data(csv_file)
        
    def load_grade_data(self, csv_file: str):
        """Load data from your CSV file with robust error handling"""
        try:
            if os.path.exists(csv_file):
                print(f"📁 Attempting to load {csv_file}...")
                
                # First, let's try to inspect the file structure
                with open(csv_file, 'r', encoding='utf-8', errors='ignore') as f:
                    first_few_lines = [f.readline().strip() for _ in range(5)]
                
                print("🔍 First few lines of file:")
                for i, line in enumerate(first_few_lines):
                    print(f"   {i+1}: {line[:100]}..." if len(line) > 100 else f"   {i+1}: {line}")
                
                # Try different parsing strategies
                parsing_strategies = [
                    # Strategy 1: Standard CSV
                    {'sep': ',', 'quoting': 0},
                    # Strategy 2: Handle quoted fields
                    {'sep': ',', 'quoting': 1, 'skipinitialspace': True},
                    # Strategy 3: More robust with error handling
                    {'sep': ',', 'quoting': 1, 'skipinitialspace': True, 'on_bad_lines': 'skip'},
                    # Strategy 4: Try semicolon separator
                    {'sep': ';', 'quoting': 1},
                    # Strategy 5: Tab separator
                    {'sep': '\t', 'quoting': 1}
                ]
                
                for i, strategy in enumerate(parsing_strategies):
                    try:
                        print(f"\n🔄 Trying parsing strategy {i+1}...")
                        self.df = pd.read_csv(csv_file, **strategy)
                        print(f"✅ Strategy {i+1} worked! Loaded {len(self.df)} rows, {len(self.df.columns)} columns")
                        print(f"📋 Columns found: {list(self.df.columns)}")
                        break
                    except Exception as strategy_error:
                        print(f"❌ Strategy {i+1} failed: {str(strategy_error)[:100]}")
                        continue
                else:
                    # If all strategies fail, try manual line-by-line parsing
                    print("\n🛠️  All standard strategies failed. Attempting manual parsing...")
                    self.df = self._manual_csv_parse(csv_file)
                
                if self.df is None or self.df.empty:
                    print("❌ Could not parse the CSV file with any method")
                    return
                
                # Now clean the data
                print(f"\n🧹 Cleaning data...")
                print(f"Original shape: {self.df.shape}")
                
                # Show first few rows before cleaning
                print("\n📋 Raw data sample:")
                print(self.df.head(3).to_string())
                
                # Identify the course column (might have different names)
                course_column = None
                possible_course_cols = ['Course', 'course', 'Class', 'class', 'Course Code', 'course_code']
                for col in possible_course_cols:
                    if col in self.df.columns:
                        course_column = col
                        break
                
                if course_column is None:
                    print("❌ Could not find a course code column")
                    print(f"Available columns: {list(self.df.columns)}")
                    return
                
                print(f"📚 Using '{course_column}' as course column")
                
                # Clean the data
                self.df = self.df.dropna(subset=[course_column])  # Remove rows without course codes
                self.df[course_column] = self.df[course_column].astype(str).str.upper().str.strip()
                
                # Standardize column name
                if course_column != 'Course':
                    self.df['Course'] = self.df[course_column]
                
                # Handle grade columns
                grade_columns = [col for col in self.df.columns if 'ave' in col.lower() or 'grade' in col.lower()]
                if grade_columns:
                    print(f"📊 Found grade columns: {grade_columns}")
                    # Use the first grade column as primary
                    self.df['Class Ave'] = pd.to_numeric(self.df[grade_columns[0]], errors='coerce')
                
                # Handle credits column
                credit_columns = [col for col in self.df.columns if 'credit' in col.lower()]
                if credit_columns:
                    self.df['Credits'] = pd.to_numeric(self.df[credit_columns[0]], errors='coerce')
                
                # Remove clearly invalid courses (like #REF! errors)
                self.df = self.df[~self.df['Course'].str.contains('#REF!|NaN|nan', case=False, na=False)]
                
                print(f"✅ Cleaned data: {len(self.df)} course records")
                print(f"📊 Unique courses: {self.df['Course'].nunique()}")
                
                # Show sample of clean data
                print("\n📋 Cleaned data sample:")
                display_cols = ['Course']
                if 'Class Ave' in self.df.columns:
                    display_cols.append('Class Ave')
                if 'Credits' in self.df.columns:
                    display_cols.append('Credits')
                if 'Term Name' in self.df.columns:
                    display_cols.append('Term Name')
                
                sample_df = self.df[display_cols].head()
                print(sample_df.to_string())
                
            else:
                print(f"❌ File {csv_file} not found!")
                
        except Exception as e:
            print(f"❌ Error loading data: {e}")
            import traceback
            traceback.print_exc()
    
    def _manual_csv_parse(self, csv_file: str):
        """Manual CSV parsing as fallback"""
        try:
            print("🔧 Attempting manual line-by-line parsing...")
            
            rows = []
            headers = None
            
            with open(csv_file, 'r', encoding='utf-8', errors='ignore') as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    
                    # Split by comma, but be smart about it
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
                    
                    # Add the last field
                    fields.append(current_field.strip().strip('"'))
                    
                    if headers is None:
                        headers = fields
                        print(f"📋 Headers found: {headers}")
                    else:
                        # Pad or truncate to match header length
                        while len(fields) < len(headers):
                            fields.append('')
                        fields = fields[:len(headers)]
                        rows.append(fields)
                    
                    # Stop if we get too many parsing errors
                    if line_num > 1000 and len(rows) < 10:
                        print("⚠️  Too many parsing errors, stopping manual parse")
                        break
            
            if headers and rows:
                df = pd.DataFrame(rows, columns=headers)
                print(f"✅ Manual parsing successful: {len(df)} rows")
                return df
            else:
                return None
                
        except Exception as e:
            print(f"❌ Manual parsing failed: {e}")
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
        
        print(f"👤 Created profile for {name}")
        print(f"🎓 Major: {major}, Year {year}")
        print(f"📈 Current GPA: {current_gpa}, Target: {target_gpa}")
        print(f"💪 Strong subjects: {', '.join(self.student_profile['strong_subjects'])}")
        
        return self.student_profile
    
    def _identify_strong_subjects(self, completed_courses: List[str]) -> List[str]:
        """Identify subjects where student has taken multiple courses"""
        subjects = []
        for course in completed_courses:
            if len(course) >= 4:
                subject = course[:4]  # First 4 letters (e.g., COMP, MATH)
                subjects.append(subject)
        
        # Count frequency and return top subjects
        subject_counts = pd.Series(subjects).value_counts()
        return subject_counts.head(3).index.tolist()
    
    def predict_student_grade(self, course_code: str) -> Tuple[float, float]:
        """
        Predict student's likely grade in a course
        Returns (predicted_grade, confidence_score)
        """
        if self.df is None or course_code.upper() not in self.df['Course'].values:
            return 0.0, 0.0
        
        # Get course data
        course_data = self.df[self.df['Course'] == course_code.upper()]
        base_average = course_data['Class Ave'].mean()
        
        if pd.isna(base_average):
            return 0.0, 0.0
        
        # Start with class average as baseline
        predicted_grade = base_average
        confidence = 0.3  # Base confidence
        
        if self.student_profile:
            # Adjust based on student's GPA relative to typical class average
            gpa_adjustment = (self.student_profile['current_gpa'] - 2.7) * 0.3  # 2.7 is roughly average
            predicted_grade += gpa_adjustment
            confidence += 0.2
            
            # Bonus if student is strong in this subject area
            course_subject = course_code[:4] if len(course_code) >= 4 else ""
            if course_subject in self.student_profile.get('strong_subjects', []):
                predicted_grade += 0.2
                confidence += 0.3
            
            # Adjust based on difficulty preference vs course difficulty
            course_difficulty = self.calculate_difficulty_score(course_code)
            pref_difficulty = self.student_profile['difficulty_preference'].value
            
            if abs(course_difficulty - pref_difficulty) <= 1:
                confidence += 0.2
            
        # Ensure grade is within valid range
        predicted_grade = max(0.0, min(4.0, predicted_grade))
        confidence = max(0.0, min(1.0, confidence))
        
        return predicted_grade, confidence
    
    def calculate_difficulty_score(self, course_code: str) -> float:
        """Calculate difficulty score (1-4) based on class averages"""
        if self.df is None:
            return 2.5
        
        course_data = self.df[self.df['Course'] == course_code.upper()]
        if course_data.empty:
            return 2.5
        
        avg_grade = course_data['Class Ave'].mean()
        if pd.isna(avg_grade):
            return 2.5
        
        # Convert grade to difficulty (inverse relationship)
        # Higher grades = lower difficulty
        if avg_grade >= 3.7:
            return 1.5  # Easy
        elif avg_grade >= 3.3:
            return 2.0  # Moderate-Easy  
        elif avg_grade >= 3.0:
            return 2.5  # Moderate
        elif avg_grade >= 2.7:
            return 3.0  # Challenging
        else:
            return 3.5  # Very Hard
    
    def get_course_recommendations(self, 
                                 num_courses: int = 8,
                                 semester: str = "Fall",
                                 exclude_completed: bool = True,
                                 min_credits: int = 3,
                                 subject_filter: List[str] = None) -> List[CourseRecommendation]:
        """Get personalized course recommendations"""
        
        if self.df is None:
            print("❌ No data loaded!")
            return []
        
        recommendations = []
        
        # Filter courses
        filtered_df = self.df.copy()
        
        # Remove completed courses
        if exclude_completed and self.student_profile:
            completed = self.student_profile.get('completed_courses', [])
            filtered_df = filtered_df[~filtered_df['Course'].isin(completed)]
        
        # Filter by subject if specified
        if subject_filter:
            subject_filter = [s.upper() for s in subject_filter]
            filtered_df = filtered_df[filtered_df['Course'].str[:4].isin(subject_filter)]
        
        # Filter by credits
        if 'Credits' in filtered_df.columns:
            filtered_df = filtered_df[filtered_df['Credits'] >= min_credits]
        
        # Get unique courses (in case there are duplicates from different terms)
        unique_courses = filtered_df.drop_duplicates('Course')
        
        print(f"🔍 Analyzing {len(unique_courses)} potential courses...")
        
        for _, course_row in unique_courses.iterrows():
            course_code = course_row['Course']
            
            if pd.isna(course_code) or not isinstance(course_code, str):
                continue
            
            # Get predictions
            predicted_grade, confidence = self.predict_student_grade(course_code)
            difficulty = self.calculate_difficulty_score(course_code)
            
            # Get course details
            class_avg = course_row.get('Class Ave', 0.0)
            credits = int(course_row.get('Credits', 3))
            term = course_row.get('Term Name', 'Unknown')
            
            # Generate reasons and warnings
            reasons = self._generate_reasons(course_code, predicted_grade, difficulty, class_avg)
            warnings = self._generate_warnings(course_code, difficulty, predicted_grade)
            
            recommendation = CourseRecommendation(
                course_code=course_code,
                course_name=f"Course {course_code}",  # You could enhance this with actual names
                predicted_grade=predicted_grade,
                difficulty_score=difficulty,
                class_average=class_avg,
                credits=credits,
                reasons=reasons,
                warnings=warnings,
                confidence=confidence,
                term_offered=term
            )
            
            recommendations.append(recommendation)
        
        # Sort by a composite score: predicted grade * confidence - difficulty penalty
        def score_recommendation(rec):
            base_score = rec.predicted_grade * rec.confidence
            # Small penalty for very easy or very hard courses
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
                
            pref_difficulty = self.student_profile['difficulty_preference'].value
            if abs(difficulty - pref_difficulty) <= 0.5:
                reasons.append("Good difficulty match for your preferences")
        
        if difficulty <= 2.0:
            reasons.append("Manageable difficulty level")
        
        return reasons
    
    def _generate_warnings(self, course_code: str, difficulty: float, pred_grade: float) -> List[str]:
        """Generate warnings about potential challenges"""
        warnings = []
        
        if difficulty >= 3.2:
            warnings.append("⚠️  High difficulty course - plan accordingly")
        
        if pred_grade < 2.7:
            warnings.append("⚠️  Below-average predicted performance")
        
        if self.student_profile:
            if self.student_profile['current_gpa'] < 3.0 and difficulty > 3.0:
                warnings.append("⚠️  May be challenging given current GPA")
        
        return warnings
    
    def display_recommendations(self, recommendations: List[CourseRecommendation]):
        """Display recommendations in a nice format"""
        print("\n" + "="*80)
        print("🎯 PERSONALIZED COURSE RECOMMENDATIONS")
        print("="*80)
        
        for i, rec in enumerate(recommendations, 1):
            print(f"\n{i}. {rec.course_code} ({rec.credits} credits)")
            print(f"   📊 Predicted Grade: {rec.predicted_grade:.1f}/4.0")
            print(f"   📈 Class Average: {rec.class_average:.1f}/4.0") 
            print(f"   💪 Difficulty: {rec.difficulty_score:.1f}/4.0")
            print(f"   🎯 Confidence: {rec.confidence:.1f}/1.0")
            
            if rec.reasons:
                print(f"   ✅ Why recommended: {', '.join(rec.reasons)}")
            
            if rec.warnings:
                print(f"   {' '.join(rec.warnings)}")
            
            print(f"   🗓️  Term: {rec.term_offered}")

# CLI Functions
def quick_test():
    """Quick test with sample data"""
    print("🧪 QUICK TEST MODE")
    print("="*50)
    
    # Initialize advisor
    advisor = McGillAdvisorAI("ClassAverageCrowdSourcing.csv")
    
    if advisor.df is None:
        print("❌ Cannot load data file!")
        return
    
    # Show data overview
    print(f"\n📊 Data Overview:")
    print(f"Total records: {len(advisor.df)}")
    print(f"Unique courses: {advisor.df['Course'].nunique()}")
    
    # Show some sample courses
    print(f"\n📋 Sample courses in dataset:")
    sample_courses = advisor.df['Course'].dropna().unique()[:10]
    for i, course in enumerate(sample_courses, 1):
        avg_grade = advisor.df[advisor.df['Course'] == course]['Class Ave'].mean()
        print(f"{i:2d}. {course:8s} (Avg: {avg_grade:.2f})")
    
    # Create a test student profile
    print(f"\n👤 Creating test student profile...")
    advisor.create_student_profile(
        name="Test Student",
        major="Computer Science",
        year=2,
        completed_courses=["COMP250", "MATH240", "COMP251"],
        current_gpa=3.2,
        target_gpa=3.5,
        interests=["Programming", "AI"],
        difficulty_preference=DifficultyLevel.MODERATE
    )
    
    # Get recommendations
    print(f"\n🎯 Getting course recommendations...")
    recommendations = advisor.get_course_recommendations(num_courses=5)
    
    if recommendations:
        advisor.display_recommendations(recommendations)
    else:
        print("❌ No recommendations generated")

def interactive_mode():
    """Interactive mode for real usage"""
    print("🎓 McGILL COURSE ADVISORY AI")
    print("="*50)
    
    advisor = McGillAdvisorAI("ClassAverageCrowdSourcing.csv")
    
    if advisor.df is None:
        print("❌ Cannot load data file!")
        return
    
    while True:
        print(f"\n🏠 Main Menu:")
        print("1. Create student profile")
        print("2. Get course recommendations") 
        print("3. Predict grade for specific course")
        print("4. View data statistics")
        print("5. Search courses by subject")
        print("6. Exit")
        
        choice = input("\nSelect option (1-6): ").strip()
        
        if choice == "1":
            create_profile_interactive(advisor)
        elif choice == "2":
            get_recommendations_interactive(advisor)
        elif choice == "3":
            predict_grade_interactive(advisor)
        elif choice == "4":
            show_data_stats(advisor)
        elif choice == "5":
            search_courses_interactive(advisor)
        elif choice == "6":
            print("👋 Goodbye!")
            break
        else:
            print("❌ Invalid option")

def create_profile_interactive(advisor):
    """Interactive profile creation"""
    print(f"\n👤 Create Student Profile:")
    
    name = input("Name: ").strip()
    major = input("Major: ").strip()
    
    try:
        year = int(input("Year (1-4): "))
        current_gpa = float(input("Current GPA (0.0-4.0): "))
    except ValueError:
        print("❌ Invalid input for year or GPA")
        return
    
    print("\nCompleted courses (comma-separated, e.g., COMP250,MATH240):")
    completed_input = input("Courses: ").strip()
    completed_courses = [c.strip().upper() for c in completed_input.split(",") if c.strip()]
    
    print("\nDifficulty preference:")
    print("1. Easy")
    print("2. Moderate") 
    print("3. Challenging")
    print("4. Very Hard")
    
    try:
        diff_choice = int(input("Choice (1-4): "))
        difficulty_map = {1: DifficultyLevel.EASY, 2: DifficultyLevel.MODERATE, 
                         3: DifficultyLevel.CHALLENGING, 4: DifficultyLevel.VERY_HARD}
        difficulty = difficulty_map.get(diff_choice, DifficultyLevel.MODERATE)
    except ValueError:
        difficulty = DifficultyLevel.MODERATE
    
    advisor.create_student_profile(
        name=name,
        major=major,
        year=year, 
        completed_courses=completed_courses,
        current_gpa=current_gpa,
        difficulty_preference=difficulty
    )

def get_recommendations_interactive(advisor):
    """Interactive recommendations"""
    if not advisor.student_profile:
        print("❌ Please create a student profile first!")
        return
    
    try:
        num_courses = int(input(f"\nHow many recommendations? (default 5): ") or "5")
        
        print(f"\nFilter by subject? (leave empty for all subjects)")
        subject_input = input("Subjects (comma-separated, e.g., COMP,MATH): ").strip()
        subject_filter = [s.strip().upper() for s in subject_input.split(",") if s.strip()] if subject_input else None
        
        recommendations = advisor.get_course_recommendations(
            num_courses=num_courses,
            subject_filter=subject_filter
        )
        
        if recommendations:
            advisor.display_recommendations(recommendations)
        else:
            print("❌ No recommendations found")
            
    except ValueError:
        print("❌ Invalid input")

def predict_grade_interactive(advisor):
    """Interactive grade prediction"""
    if not advisor.student_profile:
        print("❌ Please create a student profile first!")
        return
    
    course_code = input(f"\nEnter course code (e.g., COMP273): ").strip().upper()
    
    pred_grade, confidence = advisor.predict_student_grade(course_code)
    difficulty = advisor.calculate_difficulty_score(course_code)
    
    if pred_grade > 0:
        print(f"\n🔮 Prediction for {course_code}:")
        print(f"   📊 Predicted Grade: {pred_grade:.2f}/4.0")
        print(f"   💪 Difficulty: {difficulty:.1f}/4.0") 
        print(f"   🎯 Confidence: {confidence:.2f}/1.0")
        
        # Show class average for comparison
        course_data = advisor.df[advisor.df['Course'] == course_code]
        if not course_data.empty:
            class_avg = course_data['Class Ave'].mean()
            print(f"   📈 Historical Average: {class_avg:.2f}/4.0")
    else:
        print(f"❌ Course {course_code} not found in dataset")

def show_data_stats(advisor):
    """Show dataset statistics"""
    print(f"\n📊 Dataset Statistics:")
    print(f"Total records: {len(advisor.df)}")
    print(f"Unique courses: {advisor.df['Course'].nunique()}")
    
    # Subject breakdown
    advisor.df['Subject'] = advisor.df['Course'].str[:4]
    subject_counts = advisor.df['Subject'].value_counts().head(10)
    
    print(f"\n📚 Top subjects by course count:")
    for subject, count in subject_counts.items():
        print(f"   {subject}: {count} courses")
    
    # Grade statistics
    if 'Class Ave' in advisor.df.columns:
        avg_grades = advisor.df['Class Ave'].dropna()
        print(f"\n📈 Grade Statistics:")
        print(f"   Mean: {avg_grades.mean():.2f}")
        print(f"   Median: {avg_grades.median():.2f}")
        print(f"   Min: {avg_grades.min():.2f}")
        print(f"   Max: {avg_grades.max():.2f}")

def search_courses_interactive(advisor):
    """Search courses by subject"""
    subject = input(f"\nEnter subject code (e.g., COMP): ").strip().upper()
    
    subject_courses = advisor.df[advisor.df['Course'].str.startswith(subject)]
    
    if subject_courses.empty:
        print(f"❌ No courses found for subject {subject}")
        return
    
    print(f"\n📚 Courses in {subject}:")
    
    # Group by course and show average stats
    course_stats = subject_courses.groupby('Course').agg({
        'Class Ave': 'mean',
        'Credits': 'first'
    }).round(2)
    
    for course, stats in course_stats.head(15).iterrows():
        credits = int(stats['Credits']) if not pd.isna(stats['Credits']) else '?'
        avg = stats['Class Ave'] if not pd.isna(stats['Class Ave']) else '?'
        print(f"   {course:8s} ({credits} cr) - Avg: {avg}")

def main():
    """Main CLI entry point""" 
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        quick_test()
    else:
        interactive_mode()

if __name__ == "__main__":
    main()
