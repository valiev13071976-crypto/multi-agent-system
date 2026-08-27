# Production transport is HTTPS/TLS at the reverse proxy / platform edge.
# This application does not implement TLS itself.
PRODUCTION_TRANSPORT = "HTTPS/TLS"

# Security boundary notes (app layer):
# - TLS termination: Railway / reverse proxy (not FastAPI)
# - Do not trust client X-Forwarded-* without verified proxy config
# - HSTS / security headers: configure at reverse proxy when possible
# - Bearer/API-key auth at application layer (security.api_auth)
# - Session cookies: not used; CSRF not required for bearer-token API
