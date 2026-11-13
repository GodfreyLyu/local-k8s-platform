-- Purpose: Verify the configured application identity has no administrative or monitoring roles.
SELECT current_database()=:'app_database' AND current_user=:'app_user'
AND NOT (SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls FROM pg_roles WHERE rolname=current_user)
AND NOT pg_has_role(current_user,'pg_monitor','MEMBER');
