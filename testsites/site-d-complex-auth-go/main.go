package main

import (
  "crypto/rand"
  "encoding/hex"
  "html/template"
  "log"
  "net/http"
  "os"
  "sync"
)

type Server struct {
  templates *template.Template
  sessions  map[string]string
  mu        sync.Mutex
}

func main() {
  tmpl := template.Must(template.ParseGlob("templates/*.html"))
  server := &Server{templates: tmpl, sessions: map[string]string{}}

  mux := http.NewServeMux()
  mux.HandleFunc("/", server.handleHome)
  mux.HandleFunc("/login", server.handleLogin)
  mux.HandleFunc("/register", server.handleRegister)
  mux.HandleFunc("/app", server.requireAuth(server.handleApp))
  mux.HandleFunc("/app/billing", server.requireAuth(server.handleBilling))

  port := os.Getenv("PORT")
  if port == "" {
    port = "8000"
  }
  addr := ":" + port
  log.Printf("complex auth listening on %s", addr)
  log.Fatal(http.ListenAndServe(addr, mux))
}

func (s *Server) handleHome(w http.ResponseWriter, r *http.Request) {
  s.templates.ExecuteTemplate(w, "home.html", nil)
}

func (s *Server) handleRegister(w http.ResponseWriter, r *http.Request) {
  s.templates.ExecuteTemplate(w, "register.html", nil)
}

func (s *Server) handleLogin(w http.ResponseWriter, r *http.Request) {
  if r.Method == http.MethodPost {
    action := r.FormValue("action")
    if action == "register" {
      http.Redirect(w, r, "/register", http.StatusSeeOther)
      return
    }

    username := r.FormValue("username")
    password := r.FormValue("password")
    if username == "admin" && password == "swordfish" {
      sessionID := newSessionID()
      s.mu.Lock()
      s.sessions[sessionID] = username
      s.mu.Unlock()

      http.SetCookie(w, &http.Cookie{Name: "session_id", Value: sessionID, Path: "/"})
      http.Redirect(w, r, "/app", http.StatusSeeOther)
      return
    }

    s.templates.ExecuteTemplate(w, "login.html", map[string]string{"Error": "Incorrect credentials"})
    return
  }

  s.templates.ExecuteTemplate(w, "login.html", nil)
}

func (s *Server) handleApp(w http.ResponseWriter, r *http.Request) {
  s.templates.ExecuteTemplate(w, "app.html", map[string]string{"Section": "Overview"})
}

func (s *Server) handleBilling(w http.ResponseWriter, r *http.Request) {
  s.templates.ExecuteTemplate(w, "app.html", map[string]string{"Section": "Billing"})
}

func (s *Server) requireAuth(next http.HandlerFunc) http.HandlerFunc {
  return func(w http.ResponseWriter, r *http.Request) {
    cookie, err := r.Cookie("session_id")
    if err != nil || cookie.Value == "" {
      http.Redirect(w, r, "/login", http.StatusSeeOther)
      return
    }
    s.mu.Lock()
    _, ok := s.sessions[cookie.Value]
    s.mu.Unlock()
    if !ok {
      http.Redirect(w, r, "/login", http.StatusSeeOther)
      return
    }
    next(w, r)
  }
}

func newSessionID() string {
  data := make([]byte, 16)
  if _, err := rand.Read(data); err != nil {
    return "fallback"
  }
  return hex.EncodeToString(data)
}
