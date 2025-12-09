import axios from 'axios'
import { supabase } from './supabase'

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000/api'

// Create axios instance with auth interceptor
const api = axios.create({
  baseURL: API_URL,
  headers: {
    'Content-Type': 'application/json',
  },
})

// Add auth token to requests
api.interceptors.request.use(async (config) => {
  const session = await supabase.auth.getSession()
  if (session?.data?.session?.access_token) {
    config.headers.Authorization = `Bearer ${session.data.session.access_token}`
  }
  return config
})

// API methods
export const chatAPI = {
  sendMessage: async (userId, message) => {
    const response = await api.post('/chat/send', {
      user_id: userId,
      message: message,
    })
    return response.data
  },
  
  getHistory: async (userId, limit = 50) => {
    const response = await api.get(`/chat/history/${userId}?limit=${limit}`)
    return response.data
  },
}

export const coursesAPI = {
  search: async (query = '', subject = null, limit = 50) => {
    const params = new URLSearchParams()
    if (query) params.append('query', query)
    if (subject) params.append('subject', subject)
    params.append('limit', limit)
    
    const response = await api.get(`/courses/search?${params}`)
    return response.data
  },
  
  getDetails: async (subject, catalog) => {
    const response = await api.get(`/courses/${subject}/${catalog}`)
    return response.data
  },
  
  getSubjects: async () => {
    const response = await api.get('/courses/subjects')
    return response.data
  },
}

export const usersAPI = {
  createUser: async (userData) => {
    const response = await api.post('/users/', userData)
    return response.data
  },
  
  getUser: async (userId) => {
    const response = await api.get(`/users/${userId}`)
    return response.data
  },
  
  updateUser: async (userId, updates) => {
    const response = await api.patch(`/users/${userId}`, updates)
    return response.data
  },
}

export default api