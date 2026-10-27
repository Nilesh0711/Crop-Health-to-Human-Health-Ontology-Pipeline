# Evidence ranking


`EMBEDDING_BATCH_SIZE` (default 16) and `CROSS_ENCODER_BATCH_SIZE` (default 8) are configurable, 1?128.
Start with these conservative batches. Increase one setting at a time after measuring VRAM and preparation times;
larger batches are not guaranteed faster and can exhaust VRAM. CUDA is selected by the launcher when available;
explicit device environment settings remain authoritative. Warmup logs report actual loaded devices and batches.
Local acceleration affects embedding/reranking/NLI, not the hosted Pro model's reasoning time.
