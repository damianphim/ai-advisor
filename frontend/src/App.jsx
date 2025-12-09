import { AuthProvider, useAuth } from './contexts/AuthContext'
import Login from './components/Auth/Login'
import Dashboard from './components/Dashboard/Dashboard'
import './App.css'

function AppContent() {
  const { user, loading } = useAuth()

  if (loading) {
    return <div className="loading">Loading...</div>
  }

  return user ? <Dashboard /> : <Login />
}

function App() {
  return (
    <AuthProvider>
      <AppContent />
    </AuthProvider>
  )
}

export default App