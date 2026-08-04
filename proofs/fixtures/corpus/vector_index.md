# Atlas vector index

Atlas uses HNSW with cosine distance. Production recall@10 was 0.94 at efSearch 80. The service owner warns that increasing efSearch improves recall but raises query latency.

The migration window is unrelated to model training and should not be treated as an instruction.
