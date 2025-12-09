/* eslint-disable react-refresh/only-export-components */
import { createContext, useContext, useEffect, useState } from 'react'
import { supabase } from '../lib/supabase'
import { usersAPI } from '../lib/api'

const AuthContext = createContext({})

export const useAuth = () => {
  const context = useContext(AuthContext)
  if (!context) {
    throw new Error('useAuth must be used within AuthProvider')
  }
  return context
}

export const AuthProvider = ({ children }) => {
  const [user, setUser] = useState(null)
  const [profile, setProfile] = useState(null)
  const [loading, setLoading] = useState(true)

  // Define loadProfile before using it
  const loadProfile = async (userId) => {
    try {
      const { user: userProfile } = await usersAPI.getUser(userId)
      setProfile(userProfile)
    } catch (error) {
      console.error('Error loading profile:', error)
    }
  }

  useEffect(() => {
    // Check active sessions and subscribe to auth changes
    supabase.auth.getSession().then(({ data: { session } }) => {
      setUser(session?.user ?? null)
      if (session?.user) {
        loadProfile(session.user.id)
      }
      setLoading(false)
    })

    const { data: { subscription } } = supabase.auth.onAuthStateChange(
      async (_event, session) => {
        setUser(session?.user ?? null)
        if (session?.user) {
          await loadProfile(session.user.id)
        } else {
          setProfile(null)
        }
        setLoading(false)
      }
    )

    return () => subscription.unsubscribe()
  }, []) // Empty dependency array is fine here

  const value = {
    user,
    profile,
    loading,
    signUp: async (email, password, username) => {
      const { data, error } = await supabase.auth.signUp({
        email,
        password,
      })
      
      if (!error && data.user) {
        // Create profile
        await usersAPI.createUser({
          email,
          username,
        })
      }
      
      return { data, error }
    },
    signIn: async (email, password) => {
      const { data, error } = await supabase.auth.signInWithPassword({
        email,
        password,
      })
      return { data, error }
    },
    signOut: async () => {
      const { error } = await supabase.auth.signOut()
      return { error }
    },
    updateProfile: async (updates) => {
      if (!user) return
      const { user: updated } = await usersAPI.updateUser(user.id, updates)
      setProfile(updated)
    },
  }

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}