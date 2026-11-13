-- Purpose: Verify that the exporter identity has monitoring access without administrative privileges.
SELECT current_user='postgres_exporter' AND pg_has_role(current_user,'pg_monitor','USAGE')
AND NOT (SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls FROM pg_roles WHERE rolname=current_user);
