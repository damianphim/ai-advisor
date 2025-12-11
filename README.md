# McGill AI Academic Advisor 🎓

An intelligent, AI-powered web application designed to help McGill University students plan their academic journey. The application combines statistical analysis of historical grade data with a conversational AI interface to provide personalized course recommendations, grade predictions, and academic advice.

🌐 **Live Application**: [ai-advisor-pi.vercel.app](https://ai-advisor-pi.vercel.app)

## ✨ Features

### 🤖 AI Chat Advisor
Conversational interface powered by Claude 3.5 Sonnet to answer questions about courses, prerequisites, and career paths with context-aware responses.

### 🔐 User Authentication
Secure authentication system with persistent user profiles and preferences.

### 💾 Persistent Chat History
Automatically saves your conversation history to the cloud, allowing you to access past discussions from any device.

### 📊 Grade Prediction
Uses historical data and your current GPA to estimate your performance in future courses.

### 🎯 Smart Course Recommendations
Suggests courses based on your major, interests, and preferred difficulty level, backed by real historical data.

### 📈 Difficulty Analysis
Provides detailed course difficulty breakdowns based on crowdsourced class averages from McGill students.

### 🗂️ Interactive Course Explorer
Browse and search through McGill's course catalog with detailed information about each course.

### 🎨 McGill-Branded UI
Professional dashboard design featuring McGill's official colors and branding guidelines.

## 🛠️ Tech Stack

### Frontend
- **Framework**: React (Vite)
- **Styling**: CSS3 with McGill branding
- **HTTP Client**: Axios
- **Deployment**: Vercel

### Backend
- **Framework**: Python (FastAPI)
- **Server**: Uvicorn
- **API**: RESTful endpoints with async support

### Database
- **Production**: Supabase (PostgreSQL)
- **ORM**: SQLAlchemy (Async)
- **Driver**: AsyncPG

### AI Engine
- **Model**: Anthropic Claude 3.5 Sonnet
- **Integration**: Claude API with conversation history context

### Data Processing
- **Libraries**: Pandas, NumPy
- **Purpose**: CSV data seeding and statistical analysis

## 🚀 Getting Started

Follow these instructions to run the project locally or deploy your own instance.

### Prerequisites

- Python 3.8+
- Node.js & npm
- PostgreSQL (local) or Supabase account
- Anthropic API Key ([Get one here](https://console.anthropic.com))

### 1️⃣ Backend Setup

Navigate to the backend directory:

```bash
cd backend
```

Create and activate a virtual environment:

**Windows:**
```bash
python -m venv venv
.\venv\Scripts\activate
```

**Mac/Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

### Configuration

Create a `.env` file in the `backend/` directory:

```env
ANTHROPIC_API_KEY=sk-ant-api03-your-actual-key-here
DATABASE_URL=postgresql+asyncpg://user:password@host:port/database
```

**For Supabase:**
Get your connection string from Supabase Dashboard → Project Settings → Database and convert it to asyncpg format:
```env
DATABASE_URL=postgresql+asyncpg://postgres.[project-ref]:[password]@aws-0-[region].pooler.supabase.com:6543/postgres
```

**For Local PostgreSQL:**
```env
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost/mcgill_advisor
```

### 2️⃣ Database Setup

The application uses SQLAlchemy to automatically generate tables (`users`, `chat_messages`, `courses`) on startup. 

**Initial Seed:**
On first run, the application will automatically seed the database with course data from `ClassAverageCrowdSourcing.csv` if the courses table is empty.

**Manual Database Creation (Local PostgreSQL only):**
```sql
CREATE DATABASE mcgill_advisor;
```

### 3️⃣ Frontend Setup

In a new terminal, navigate to the frontend directory:

```bash
cd frontend
```

Install dependencies:

```bash
npm install
```

### Configuration

Update the API endpoint in your frontend code if needed. For local development, ensure the backend URL points to `http://localhost:8000`.

## 🏃‍♂️ Running the Application

You need to run both the backend and frontend simultaneously.

### Terminal 1 (Backend):

```bash
cd backend
# Ensure venv is active
uvicorn main:app --reload
```

Server will start at `http://localhost:8000`

### Terminal 2 (Frontend):

```bash
cd frontend
npm run dev
```

Client will start at `http://localhost:5173`

Open your browser to `http://localhost:5173` to use the advisor!

## 🌐 Deployment

### Backend (Railway/Render/Fly.io)

1. Connect your GitHub repository
2. Set environment variables:
   - `ANTHROPIC_API_KEY`
   - `DATABASE_URL` (Supabase connection string)
3. Deploy from `backend` directory

### Frontend (Vercel)

1. Import your GitHub repository to Vercel
2. Set the root directory to `frontend`
3. Build command: `npm run build`
4. Output directory: `dist`
5. Add environment variable for API URL if needed

### Database (Supabase)

1. Create a new Supabase project
2. Copy the connection string from Project Settings → Database
3. Update `DATABASE_URL` in your backend environment variables

## 📁 Project Structure

```
ai-advisor/
├── backend/
│   ├── main.py                 # FastAPI app and endpoints
│   ├── database.py             # SQLAlchemy models and connection
│   ├── requirements.txt        # Python dependencies
│   ├── .env                    # Environment variables (not tracked)
│   └── ClassAverageCrowdSourcing.csv
├── frontend/
│   ├── src/
│   │   ├── components/        # React components
│   │   ├── App.jsx           # Main application component
│   │   └── App.css           # Styles with McGill branding
│   ├── package.json
│   └── vite.config.js
├── .gitignore
├── LICENSE
└── README.md
```

## 🧪 Future Roadmap

- [ ] **Prerequisite Checking**: Implement a DAG (Directed Acyclic Graph) to validate course requirements
- [ ] **Machine Learning Enhancement**: Replace heuristic grade prediction with Scikit-Learn regression models
- [ ] **Enhanced Authentication**: Add password reset, email verification, and OAuth providers
- [ ] **Mobile Optimization**: Responsive design improvements for mobile devices
- [ ] **Course Schedule Builder**: Visual semester planning with conflict detection
- [ ] **Peer Reviews**: Community-driven course reviews and ratings

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- Course data sourced from McGill student crowdsourcing efforts
- Powered by Anthropic's Claude AI
- Built for McGill University students

## 📧 Contact

For questions or feedback, please open an issue on GitHub.

---

Made with ❤️ for McGill students
