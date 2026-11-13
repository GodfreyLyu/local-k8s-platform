-- Purpose: Confirm database ownership and approved role membership before exposing the database.
SELECT EXISTS (SELECT FROM pg_database d JOIN pg_roles r ON r.oid=d.datdba WHERE d.datname=:'app_database' AND r.rolname=:'app_user')
AND NOT EXISTS (SELECT FROM pg_auth_members m JOIN pg_roles member_role ON member_role.oid=m.member JOIN pg_roles parent_role ON parent_role.oid=m.roleid WHERE member_role.rolname=:'app_user' OR (member_role.rolname='postgres_exporter' AND parent_role.rolname<>'pg_monitor'));
