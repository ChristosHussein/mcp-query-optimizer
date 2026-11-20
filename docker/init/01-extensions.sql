-- Enable extensions in template1 so all newly created databases
-- (imdb, tpch, and eval clones) inherit them automatically.
\c template1
CREATE EXTENSION IF NOT EXISTS hypopg;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Also enable in default postgres database
\c postgres
CREATE EXTENSION IF NOT EXISTS hypopg;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Create benchmark databases
CREATE DATABASE imdb;
CREATE DATABASE tpch;
