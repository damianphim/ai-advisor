#!/usr/bin/env python3
"""
McGill AI Chatbot - Conversational Interface
Version with multiple model fallbacks
"""

import os
import sys
from typing import Dict, List, Optional
from dotenv import load_dotenv
import anthropic

# Load environment variables
load_dotenv()

# Import your existing advisor
from mcgill_advisor import McGillAdvisorAI, DifficultyLevel, CourseRecommendation

class McGillChatBot:
    """
    Conversational McGill AI Assistant using Claude
    """
    
    def __init__(self):
        # Initialize Anthropic client
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            print("⚠️  Warning: No ANTHROPIC_API_KEY found in .env file")
            print("   The chatbot will not work without an API key.")
            print("   Either add your key to .env or use: python mcgill_advisor.py")
            sys.exit(1)
            
        self.client = anthropic.Anthropic(api_key=api_key)
        
        # Initialize your existing course advisor
        print("🔄 Initializing advisor...")
        self.advisor = McGillAdvisorAI()
        
        # Track conversation state
        self.conversation_history = []
        self.student_context = {}
        
        print("🤖 McGill AI Assistant initialized!")
        print("💡 Tip: You can ask me anything about courses, careers, schedules, or McGill life!")
    
    def chat(self, user_message: str) -> str:
        """Main chat interface"""
        try:
            # Build context for Claude
            context = self._build_context(user_message)
            
            # Get Claude's response
            response = self._get_claude_response(user_message, context)
            
            # Add to conversation history
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": response})
            
            return response
            
        except Exception as e:
            error_msg = f"Sorry, I encountered an error: {e}"
            print(f"\n❌ Error details: {e}")
            return error_msg
    
    def _build_context(self, user_message: str) -> str:
        """Build relevant context based on user message"""
        context_parts = []
        
        # Always include basic McGill info
        if self.advisor.df is not None:
            context_parts.append(f"McGill Course Database: {len(self.advisor.df)} courses, {self.advisor.df['Course'].nunique()} unique courses")
        
        # Add student profile if available
        if self.advisor.student_profile:
            profile = self.advisor.student_profile
            context_parts.append(f"Student: {profile.get('name', 'Student')} - {profile.get('major', 'Unknown')} Year {profile.get('year', '?')}, GPA: {profile.get('current_gpa', '?')}")
            completed = profile.get('completed_courses', [])
            if completed:
                context_parts.append(f"Completed: {', '.join(completed[:8])}")
            strong = profile.get('strong_subjects', [])
            if strong:
                context_parts.append(f"Strong in: {', '.join(strong)}")
        
        # Check if user mentions courses
        if self._mentions_courses(user_message):
            course_info = self._get_course_info(user_message)
            if course_info:
                context_parts.append(f"Course Data:\n{course_info}")
        
        # Check if user wants recommendations
        if any(word in user_message.lower() for word in ['recommend', 'suggest', 'should i take', 'what courses', 'next semester']):
            if self.advisor.student_profile and self.advisor.df is not None:
                try:
                    recs = self.advisor.get_course_recommendations(num_courses=3)
                    if recs:
                        rec_summary = self._format_recommendations(recs)
                        context_parts.append(f"Top Recommendations:\n{rec_summary}")
                except Exception:
                    pass
        
        return "\n\n".join(context_parts) if context_parts else "No specific context available."
    
    def _mentions_courses(self, message: str) -> bool:
        """Check if message mentions specific courses"""
        words = message.upper().split()
        for word in words:
            word = word.strip('.,!?;:')
            if len(word) >= 7 and word[:4].isalpha() and word[4:7].isdigit():
                return True
        return False
    
    def _get_course_info(self, message: str) -> str:
        """Extract course information mentioned in message"""
        if self.advisor.df is None:
            return ""
            
        words = message.upper().split()
        course_info = []
        
        for word in words:
            word = word.strip('.,!?;:')
            if len(word) >= 7 and word[:4].isalpha() and word[4:7].isdigit():
                course_code = word[:7]
                if course_code in self.advisor.df['Course'].values:
                    try:
                        difficulty = self.advisor.calculate_difficulty_score(course_code)
                        pred_grade, confidence = self.advisor.predict_student_grade(course_code)
                        
                        info = f"{course_code}: Difficulty {difficulty:.1f}/4.0"
                        if pred_grade > 0:
                            info += f", Predicted {pred_grade:.2f}"
                        course_info.append(info)
                    except:
                        pass
        
        return "\n".join(course_info)
    
    def _format_recommendations(self, recommendations: List[CourseRecommendation]) -> str:
        """Format course recommendations for Claude"""
        formatted = []
        for i, rec in enumerate(recommendations[:3], 1):
            formatted.append(f"{i}. {rec.course_code}: Pred {rec.predicted_grade:.1f}, Diff {rec.difficulty_score:.1f}")
        return "\n".join(formatted)
    
    def _get_claude_response(self, user_message: str, context: str) -> str:
        """Get response from Claude - tries multiple models"""
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

        # Build messages
        messages = []
        
        # Add recent history (limit to last 4)
        recent = self.conversation_history[-4:] if len(self.conversation_history) > 4 else self.conversation_history
        messages.extend(recent)
        
        # Add current message with context
        current = f"Context:\n{context}\n\nStudent: {user_message}"
        messages.append({"role": "user", "content": current})
        
        # Try multiple models
        models = [
            "claude-3-5-sonnet-20241022",
            "claude-3-5-sonnet-20240620",
            "claude-3-opus-20240229",
            "claude-3-sonnet-20240229",
        ]
        
        print("\n🤖 McGill AI: ", end="", flush=True)
        
        last_error = None
        for i, model in enumerate(models):
            try:
                if i > 0:
                    print(f"\n⚠️  Trying model {i+1}/{len(models)}...", end="", flush=True)
                
                response = self.client.messages.create(
                    model=model,
                    max_tokens=800,
                    temperature=0.7,
                    system=system_prompt,
                    messages=messages,
                    timeout=30.0
                )
                
                response_text = response.content[0].text
                print(response_text)
                return response_text
                
            except anthropic.NotFoundError as e:
                last_error = e
                continue
                
            except anthropic.APITimeoutError:
                error_msg = "⏱️ Request timed out. Please try again."
                print(error_msg)
                return error_msg
                
            except anthropic.APIError as e:
                error_msg = f"❌ API Error: {e}"
                print(error_msg)
                return error_msg
                
            except Exception as e:
                error_msg = f"❌ Error: {e}"
                print(error_msg)
                return error_msg
        
        # All models failed
        error_msg = f"\n❌ Could not connect to any Claude model. Your API key may not have access.\nLast error: {last_error}"
        print(error_msg)
        return error_msg
    
    def create_student_profile_conversational(self, name: str, major: str, year: int, gpa: float, completed_courses: List[str]):
        """Create student profile from conversation"""
        self.advisor.create_student_profile(
            name=name,
            major=major,
            year=year,
            completed_courses=completed_courses,
            current_gpa=gpa,
            difficulty_preference=DifficultyLevel.MODERATE
        )
        self.student_context = {
            'name': name,
            'major': major,
            'year': year,
            'gpa': gpa,
            'completed_courses': completed_courses
        }

def interactive_chat():
    """Main interactive chat interface"""
    print("🎓 Welcome to McGill AI Assistant!")
    print("="*50)
    print("I'm here to help with course planning, career advice, and anything McGill-related!")
    print("Type 'quit' to exit, 'menu' for the original interface, or 'profile' to set up your profile.")
    print()
    
    try:
        chatbot = McGillChatBot()
    except SystemExit:
        return
    
    while True:
        try:
            user_input = input("\nYou: ").strip()
            
            if user_input.lower() in ['quit', 'exit', 'bye']:
                print("\n🤖 Thanks for chatting! Good luck with your studies! 🎓")
                break
            
            elif user_input.lower() == 'menu':
                print("\n📋 Opening original interface...")
                from mcgill_advisor import interactive_mode
                interactive_mode()
                continue
            
            elif user_input.lower() == 'profile':
                setup_profile_interactive(chatbot)
                continue
            
            elif user_input.lower() == 'help':
                print_help()
                continue
            
            elif not user_input:
                continue
            
            # Get response from chatbot
            chatbot.chat(user_input)
            
        except KeyboardInterrupt:
            print("\n\n🤖 Thanks for chatting! Good luck with your studies! 🎓")
            break
        except Exception as e:
            print(f"\n❌ Error: {e}")
            print("Please try again or type 'help' for assistance.\n")

def setup_profile_interactive(chatbot):
    """Interactive profile setup"""
    print("\n👤 Let's set up your student profile!")
    
    try:
        name = input("Name? ").strip()
        major = input("Major? ").strip()
        year = int(input("Year (1-4)? "))
        gpa = float(input("GPA (0.0-4.0)? "))
        
        print("Completed courses (one per line, empty line to finish):")
        completed_courses = []
        while True:
            course = input("Course: ").strip().upper()
            if not course:
                break
            completed_courses.append(course)
        
        chatbot.create_student_profile_conversational(name, major, year, gpa, completed_courses)
        print(f"\n✅ Profile created! Hi {name}!\n")
        
    except ValueError:
        print("❌ Invalid input. Try again.")
    except KeyboardInterrupt:
        print("\n⏹️  Cancelled.")

def print_help():
    """Print help information"""
    print("""
🆘 McGill AI Assistant Help

I can help with:
- Course recommendations
- Grade predictions
- Career advice
- Schedule planning
- McGill information

Commands:
- 'profile' - Set up your profile
- 'menu' - Original interface  
- 'help' - This message
- 'quit' - Exit

Just ask me anything!
""")

def quick_demo():
    """Quick demo mode"""
    print("🧪 DEMO MODE")
    print("="*50)
    
    try:
        chatbot = McGillChatBot()
    except SystemExit:
        return
    
    chatbot.create_student_profile_conversational(
        name="Demo Student",
        major="Computer Science", 
        year=2,
        gpa=3.2,
        completed_courses=["COMP250", "MATH240"]
    )
    
    questions = [
        "Hi! What courses should I take?",
        "How would I do in COMP273?",
    ]
    
    for q in questions:
        print(f"\n👤 {q}")
        chatbot.chat(q)
        print("-" * 60)

def main():
    """Main entry point"""
    if len(sys.argv) > 1:
        if sys.argv[1] == "demo":
            quick_demo()
        elif sys.argv[1] == "test":
            from mcgill_advisor import quick_test
            quick_test()
        else:
            print("Usage: python mcgill_chatbot.py [demo|test]")
    else:
        interactive_chat()

if __name__ == "__main__":
    main()

