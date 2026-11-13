-- Purpose: Initialize application and monitoring roles in fresh PGDATA using quoted psql variables.
SET password_encryption = 'scram-sha-256';
SET client_min_messages = warning;
SELECT format('CREATE ROLE %I LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 30', :'app_user')
\gexec
CREATE ROLE postgres_exporter LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 3;
SELECT format('ALTER ROLE postgres PASSWORD %L', pg_read_file('/secrets/admin/password'))
\gexec
SELECT format('ALTER ROLE %I PASSWORD %L', :'app_user', pg_read_file('/secrets/app/password'))
\gexec
SELECT format('ALTER ROLE postgres_exporter PASSWORD %L', pg_read_file('/secrets/metrics/password'))
\gexec
SELECT format('CREATE DATABASE %I OWNER %I ENCODING ''UTF8''', :'app_database', :'app_user')
\gexec
GRANT pg_monitor TO postgres_exporter;
SELECT format('GRANT CONNECT ON DATABASE %I TO postgres_exporter', :'app_database')
\gexec
