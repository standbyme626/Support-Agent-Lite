-- SQLite 3.35+ supports DROP COLUMN; older versions need table rebuild.
ALTER TABLE notification_outbox DROP COLUMN card;
