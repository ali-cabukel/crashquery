-- Runs once, on first container start.
--
-- The agent NEVER connects as the owner. It gets a role that is physically
-- incapable of writing, so a prompt injection in a column value cannot cause
-- damage even if every application-layer guard is bypassed. Application guards
-- are for good error messages; this is the actual security boundary.

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rsa_agent') THEN
    CREATE ROLE rsa_agent WITH LOGIN PASSWORD 'rsa_agent_pw';
  END IF;
END
$$;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE road_safety TO rsa_agent;
GRANT USAGE ON SCHEMA public TO rsa_agent;

-- Read-only on everything that exists now and everything created later.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO rsa_agent;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO rsa_agent;

-- Belt and braces: no temp objects, no function creation.
REVOKE TEMPORARY ON DATABASE road_safety FROM rsa_agent;

-- Any query the agent runs is killed after 30s. Prevents a badly generated
-- cartesian join from pinning the database.
ALTER ROLE rsa_agent SET statement_timeout = '30s';
ALTER ROLE rsa_agent SET idle_in_transaction_session_timeout = '10s';
ALTER ROLE rsa_agent SET default_transaction_read_only = on;
