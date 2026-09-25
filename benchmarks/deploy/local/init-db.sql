-- The same two databases the in-region job creates on RDS: the vectors, and the server's own tables.
CREATE DATABASE mem0_bench;
CREATE DATABASE mem0_bench_app;
\connect mem0_bench
CREATE EXTENSION IF NOT EXISTS vector;
