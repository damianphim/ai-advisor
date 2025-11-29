McGill AI Academic Advisor 🎓

An intelligent, AI-powered web application designed to help McGill University students plan their academic journey.

The application combines statistical analysis of historical grade data with a conversational AI interface to provide personalized course recommendations, grade predictions, and academic advice.

✨ Features

🤖 AI Chat Advisor: A conversational interface powered by Claude 3.5 Sonnet to answer questions about courses, prerequisites, and career paths.

📊 Grade Prediction: Uses historical data and your current GPA to estimate your performance in future courses.

🎯 Smart Recommendations: Suggests courses based on your major, interests, and preferred difficulty level.

📈 Difficulty Analysis: Breaks down course difficulty based on crowdsourced class averages.

🛠️ Tech Stack

Frontend: React (Vite), CSS3 (Flexbox/Grid), Axios

Backend: Python (FastAPI), Uvicorn

Data Science: Pandas, NumPy

AI Engine: Anthropic Claude API (Sonnet 3.5 / Haiku)

🚀 Getting Started

Follow these instructions to get a copy of the project up and running on your local machine.

Prerequisites

Python 3.8+

Node.js & npm (for the frontend)

Anthropic API Key (You need a key from console.anthropic.com)

1️⃣ Backend Setup

Navigate to the backend directory:

```cd backend```


Create a virtual environment:

# Windows
```
python -m venv venv
.\venv\Scripts\activate

# Mac/Linux
python3 -m venv venv
source venv/bin/activate
```

Install dependencies:

```pip install -r requirements.txt```


Configuration:
Create a file named ```.env``` inside the backend/ folder and add your API key:

```ANTHROPIC_API_KEY=sk-ant-api03-your-actual-key-here```


2️⃣ Frontend Setup

Open a new terminal window (keep the backend terminal open) and navigate to the frontend directory:

```cd frontend```


Install Node dependencies:

```npm install```


🏃‍♂️ How to Run the App

You need to run both the Backend (Server) and Frontend (Client) simultaneously.

Terminal 1 (Backend):

```
cd backend
# Ensure venv is active
uvicorn main:app --reload
```

The server will start at http://localhost:8000

Terminal 2 (Frontend):

```
cd frontend
npm run dev
```

The client will start at http://localhost:5173

Open your browser to http://localhost:5173 to start using the advisor!

🧪 Future Roadmap

[ ] Prerequisite Checking: Implement a DAG (Directed Acyclic Graph) to ensure students meet course requirements.

[ ] Machine Learning Overhaul: Replace current heuristic grade prediction with a Scikit-Learn Regression model.

[ ] User Accounts: Save student profiles and chat history to a database.

📄 License

This project is licensed under the MIT License - see the LICENSE file for details.
