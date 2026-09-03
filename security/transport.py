# Production transport is HTTPS/TLS at the reverse proxy / platform edge.
# This application does not implement TLS itself.
PRODUCTION_TRANSPORT = "HTTPS/TLS"

# Security boundary notes (app layer):
# - TLS termination: Railway / reverse proxy (not FastAPI)
# - Do not trust client X-Forwarded-* without verified proxy config
# - HSTS / security headers: configure at reverse proxy when possible
# - Dual auth: machine/workspace API-key (security.api_auth) + human session cookie (accounts.dual_auth)
# - Human sessions: HttpOnly cookie `panda_session` + CSRF token for state-changing routes
# - Do not store human passwords or session secrets in localStorage
