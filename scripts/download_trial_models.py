"""Download pinned public weights for the local trial without executing remote model code."""

from huggingface_hub import snapshot_download

for model, revision in [
    ('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2', 'e8f8c211226b894fcb81acc59f3b34ba3efd5f42'),
    ('Qwen/Qwen3-0.6B', 'c1899de289a04d12100db370d81485cdf75e47ca'),
]:
    print(snapshot_download(model, revision=revision, cache_dir='work/models',
                            allow_patterns=['*.json', '*.safetensors', '*.txt', '*.model'],
                            ignore_patterns=['onnx/*', 'openvino/*'], max_workers=2))
