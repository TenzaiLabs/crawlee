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
	mux.HandleFunc("/logout", server.requireAuth(server.handleLogout))
	mux.HandleFunc("/app", server.requireAuth(server.handleApp))
	mux.HandleFunc("/app/billing", server.requireAuth(server.handleBilling))
	mux.HandleFunc("/app/actions", server.requireAuth(server.handleActions))
	mux.HandleFunc("/app/actions/create", server.requireAuth(server.handleActionCreate))
	mux.HandleFunc("/app/actions/update", server.requireAuth(server.handleActionUpdate))
	mux.HandleFunc("/app/actions/delete", server.requireAuth(server.handleActionDelete))

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

func (s *Server) handleActions(w http.ResponseWriter, r *http.Request) {
	s.templates.ExecuteTemplate(w, "actions.html", nil)
}

func (s *Server) handleActionCreate(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Redirect(w, r, "/app/actions", http.StatusSeeOther)
		return
	}
	title := formValue(r, "title", "New control entry")
	owner := formValue(r, "owner", "ops@example.test")
	s.templates.ExecuteTemplate(w, "action_result.html", map[string]string{
		"Action":  "Created",
		"Summary": "Created " + title + " for " + owner + ".",
	})
}

func (s *Server) handleActionUpdate(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Redirect(w, r, "/app/actions", http.StatusSeeOther)
		return
	}
	entryID := formValue(r, "entry_id", "compass-001")
	status := formValue(r, "status", "Active")
	s.templates.ExecuteTemplate(w, "action_result.html", map[string]string{
		"Action":  "Updated",
		"Summary": "Updated " + entryID + " to " + status + ".",
	})
}

func (s *Server) handleActionDelete(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Redirect(w, r, "/app/actions", http.StatusSeeOther)
		return
	}
	entryID := formValue(r, "entry_id", "compass-001")
	s.templates.ExecuteTemplate(w, "action_result.html", map[string]string{
		"Action":  "Deleted",
		"Summary": "Marked " + entryID + " for deletion review.",
	})
}

func (s *Server) handleLogout(w http.ResponseWriter, r *http.Request) {
	cookie, err := r.Cookie("session_id")
	if err == nil && cookie.Value != "" {
		s.mu.Lock()
		delete(s.sessions, cookie.Value)
		s.mu.Unlock()
	}
	http.SetCookie(w, &http.Cookie{Name: "session_id", Value: "", Path: "/", MaxAge: -1})
	http.Redirect(w, r, "/", http.StatusSeeOther)
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

func formValue(r *http.Request, name string, fallback string) string {
	value := r.FormValue(name)
	if value == "" {
		return fallback
	}
	return value
}

func newSessionID() string {
	data := make([]byte, 16)
	if _, err := rand.Read(data); err != nil {
		return "fallback"
	}
	return hex.EncodeToString(data)
}
