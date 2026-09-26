-- Mem0's two databases, in its own PostgreSQL on the benchmark's host: the vectors, and the server's own tables.
CREATE DATABASE mem0_bench;
CREATE DATABASE mem0_bench_app;
\connect mem0_bench
CREATE EXTENSION IF NOT EXISTS vector;
