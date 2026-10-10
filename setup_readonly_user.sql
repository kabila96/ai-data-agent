-- =============================================================================
-- setup_readonly_user.sql
-- Creates a restricted, read-only PostgreSQL role for the AI data agent.
--
-- Run ONCE as an administrator (e.g. postgres) connected to powell_analytics:
--
--   psql -U postgres -d powell_analytics -v agent_password="'YourStrongPassword'" -f setup_readonly_user.sql
--
-- (in pgAdmin: open the Query Tool on powell_analytics, replace :agent_password
--  below with a quoted password literal, and run the script.)
--
-- Design:
--   powell_readonly  NOLOGIN group role that owns the SELECT grants
--   powell_agent_ro  LOGIN role used by the application; inherits powell_readonly
--
-- The application refuses to start analysis if it detects superuser or write
-- privileges (see database/engine.py: security_self_check).
-- Safe to re-run: grants are idempotent and the role password is reset.
-- =============================================================================

\set ON_ERROR_STOP on

-- 1. Roles ---------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'powell_readonly') THEN
        CREATE ROLE powell_readonly NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'powell_agent_ro') THEN
        CREATE ROLE powell_agent_ro LOGIN;
    END IF;
END
$$;

ALTER ROLE powell_agent_ro WITH LOGIN NOCREATEDB NOCREATEROLE INHERIT CONNECTION LIMIT 10
    PASSWORD :agent_password;

-- SUPERUSER / REPLICATION / BYPASSRLS can only be touched by a superuser, even to switch
-- them off. Managed clouds (Neon, Supabase, RDS) give you no superuser, and there a new
-- role never has these flags anyway, so only clear them when they are actually set.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'powell_agent_ro'
               AND (rolsuper OR rolreplication OR rolbypassrls)) THEN
        EXECUTE 'ALTER ROLE powell_agent_ro WITH NOSUPERUSER NOREPLICATION NOBYPASSRLS';
    END IF;
END
$$;

GRANT powell_readonly TO powell_agent_ro;

-- 2. Session defaults enforced by the server -------------------------------------
ALTER ROLE powell_agent_ro SET default_transaction_read_only = on;
ALTER ROLE powell_agent_ro SET statement_timeout = '30s';
ALTER ROLE powell_agent_ro SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE powell_agent_ro SET lock_timeout = '5s';
ALTER ROLE powell_agent_ro SET search_path = public;

-- 3. Database-level privileges -----------------------------------------------------
DO $$ BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO powell_readonly', current_database());
END $$;
-- PostgreSQL 15+ already removes CREATE on public from PUBLIC; repeat it to be explicit.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- 4. Schema-level SELECT grants ----------------------------------------------------
-- Approved schemas (matched case-insensitively, so "Salesdata" and "salesdata" both work).
-- Keep this list in sync with APPROVED_SCHEMAS in .env.
DO $$
DECLARE
    approved text[] := ARRAY['salesdata', 'data_science', 'gis', 'public', 'training'];
    s record;
BEGIN
    FOR s IN
        SELECT nspname FROM pg_namespace WHERE lower(nspname) = ANY (approved)
    LOOP
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO powell_readonly', s.nspname);
        EXECUTE format('REVOKE CREATE ON SCHEMA %I FROM powell_readonly, powell_agent_ro', s.nspname);
        EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO powell_readonly', s.nspname);
        -- Future tables created by the role running this script will also be readable.
        EXECUTE format(
            'ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT SELECT ON TABLES TO powell_readonly',
            s.nspname);
        RAISE NOTICE 'Granted read-only access to schema %', s.nspname;
    END LOOP;
END
$$;

-- 5. Remove anything that is not SELECT, in case of earlier manual grants ----------
DO $$
DECLARE
    approved text[] := ARRAY['salesdata', 'data_science', 'gis', 'public', 'training'];
    s record;
BEGIN
    FOR s IN SELECT nspname FROM pg_namespace WHERE lower(nspname) = ANY (approved)
    LOOP
        EXECUTE format(
            'REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA %I FROM powell_readonly, powell_agent_ro',
            s.nspname);
        EXECUTE format(
            'REVOKE USAGE, UPDATE ON ALL SEQUENCES IN SCHEMA %I FROM powell_readonly, powell_agent_ro',
            s.nspname);
    END LOOP;
END
$$;

-- 6. Make sure the role is not in any privileged built-in role ------------------------
DO $$
DECLARE r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['pg_write_all_data', 'pg_read_server_files', 'pg_write_server_files',
                             'pg_execute_server_program', 'pg_signal_backend']
    LOOP
        IF pg_has_role('powell_agent_ro', r, 'MEMBER') THEN
            EXECUTE format('REVOKE %I FROM powell_agent_ro', r);
            RAISE NOTICE 'Revoked membership of %', r;
        END IF;
    END LOOP;
END
$$;

-- 7. Verification (read the output) ----------------------------------------------------
SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolcanlogin, rolconnlimit, rolconfig
FROM pg_roles WHERE rolname IN ('powell_readonly', 'powell_agent_ro');

SELECT table_schema, count(*) AS selectable_tables
FROM information_schema.role_table_grants
WHERE grantee = 'powell_readonly' AND privilege_type = 'SELECT'
GROUP BY table_schema ORDER BY table_schema;

-- Optional: to block a sensitive table inside an approved schema, run
--   REVOKE SELECT ON <schema>.<table> FROM powell_readonly;
-- and also list it in BLOCKED_TABLES in .env.
